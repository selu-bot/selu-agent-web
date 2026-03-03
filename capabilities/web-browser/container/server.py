"""
Selu Web Browser Capability — gRPC server with Playwright-based browser automation.

Maintains a persistent Chromium browser instance across Invoke calls within a
session.  Each tool call returns structured text that the LLM can reason about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from concurrent import futures
from typing import Any, Callable
from urllib.parse import urlparse

import grpc
import capability_pb2
import capability_pb2_grpc

from playwright.sync_api import (
    sync_playwright,
    Browser as SyncBrowser,
    BrowserContext as SyncBrowserContext,
    Page as SyncPage,
    Playwright as SyncPlaywright,
    TimeoutError as PlaywrightTimeout,
)
from playwright.async_api import (
    async_playwright,
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Page as AsyncPage,
    Playwright as AsyncPlaywright,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("web-browser")

# ---------------------------------------------------------------------------
# Playwright thread — a dedicated thread with NO asyncio event loop.
#
# Recent versions of grpcio create an asyncio event loop on the main thread
# (and gRPC worker threads inherit it).  Playwright's sync_playwright()
# cannot start inside an existing asyncio loop.  We solve this by running
# ALL Playwright/browser work on a single dedicated thread whose asyncio
# event loop has been explicitly removed.
#
# Callers use run_in_pw_thread(fn) to execute a callable on this thread and
# block until it returns.
# ---------------------------------------------------------------------------

_pw_queue: list[tuple[Callable, threading.Event, list]] = []
_pw_queue_lock = threading.Lock()
_pw_queue_ready = threading.Event()
_pw_thread_started = threading.Event()


def _pw_thread_main() -> None:
    """Entry point for the Playwright worker thread."""
    # Guarantee this thread has NO asyncio event loop.
    #
    # Playwright's sync_playwright() checks asyncio.get_running_loop() and
    # refuses to start if one exists.  Various libraries (grpcio, uvloop,
    # container init systems) may install or run an event loop on the main
    # thread which can leak into child threads depending on the event loop
    # policy.
    #
    # We install a fresh DefaultEventLoopPolicy so this thread's state is
    # completely independent, then explicitly clear any event loop.
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    asyncio.set_event_loop(None)

    # Double-check: ensure no running loop is visible to this thread.
    try:
        asyncio.get_running_loop()
        # If we get here, something very unusual is happening.
        log.warning("Playwright thread still sees a running asyncio loop!")
    except RuntimeError:
        pass  # Expected — no running loop.

    _pw_thread_started.set()
    log.info("Playwright worker thread started.")

    while True:
        _pw_queue_ready.wait()
        _pw_queue_ready.clear()

        while True:
            with _pw_queue_lock:
                if not _pw_queue:
                    break
                fn, done_event, result_box = _pw_queue.pop(0)

            try:
                result_box.append(("ok", fn()))
            except Exception as exc:
                result_box.append(("err", exc))
            done_event.set()


_pw_thread = threading.Thread(target=_pw_thread_main, daemon=True)
_pw_thread.start()
_pw_thread_started.wait()


def run_in_pw_thread(fn: Callable) -> Any:
    """Execute *fn* on the Playwright thread and return its result.

    Blocks the calling thread until *fn* completes.  If *fn* raises,
    the exception is re-raised in the caller.
    """
    done = threading.Event()
    result_box: list = []

    with _pw_queue_lock:
        _pw_queue.append((fn, done, result_box))
    _pw_queue_ready.set()

    done.wait()

    status, value = result_box[0]
    if status == "err":
        raise value
    return value


# ---------------------------------------------------------------------------
# Session state store — simple in-memory dict, persists across Invoke calls
# ---------------------------------------------------------------------------

class SessionState:
    """Thread-safe key-value store for session state."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def size(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# Browser manager — lazy-initialised, one browser per container
# ---------------------------------------------------------------------------

class BrowserManager:
    """Manages a single Playwright browser instance and page.

    All methods MUST be called from the Playwright worker thread
    (via run_in_pw_thread).
    """

    def __init__(self) -> None:
        self._pw: SyncPlaywright | AsyncPlaywright | None = None
        self._browser: SyncBrowser | AsyncBrowser | None = None
        self._context: SyncBrowserContext | AsyncBrowserContext | None = None
        self._page: SyncPage | AsyncPage | None = None
        self._cdp_session = None
        self._proxy_username: str | None = None
        self._proxy_password: str | None = None
        self._element_map: dict[int, dict] = {}
        self._async_mode: bool = False
        self._async_loop: asyncio.AbstractEventLoop | None = None

    def _ensure_browser(self) -> SyncPage | AsyncPage:
        """Lazily start the browser on first use."""
        if self._page is not None:
            return self._page

        log.info("Starting Playwright and launching Chrome...")

        # Try the sync API first.  If an asyncio event loop is running on
        # this thread (e.g. injected by grpcio, uvloop, or the container
        # runtime), sync_playwright() will raise.  In that case, fall back
        # to the async API driven by a private event loop.
        try:
            self._pw = sync_playwright().start()
            log.info("Using Playwright Sync API.")
        except Exception as sync_err:
            if "Sync API inside the asyncio loop" in str(sync_err):
                log.warning(
                    "sync_playwright() blocked by asyncio loop; "
                    "falling back to async API: %s", sync_err,
                )
                self._async_mode = True
                self._async_loop = asyncio.new_event_loop()
                # Run the entire async setup in one shot so the event loop
                # is running for all Playwright async operations (including
                # event subscriptions like page.on()).
                self._async_loop.run_until_complete(self._async_setup())
                log.info("Browser ready (async mode).")
                return self._page
            else:
                raise

        # ----- Sync path: same setup as the original code --------------------
        self._launch_browser()
        log.info("Browser ready.")
        return self._page

    async def _async_setup(self) -> None:
        """Full browser setup using the async Playwright API."""
        self._pw = await async_playwright().start()
        log.info("Using Playwright Async API (fallback).")

        proxy_url, launch_args = self._get_proxy_and_launch_args()

        self._browser = await self._pw.chromium.launch(
            headless=True,
            channel="chrome",
            args=launch_args,
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 960},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )

        self._context.set_default_timeout(30_000)
        self._context.set_default_navigation_timeout(30_000)

        self._page = await self._context.new_page()

        async def _dismiss(d):
            await d.dismiss()
        self._page.on("dialog", _dismiss)

        if self._proxy_username:
            await self._async_setup_cdp_proxy_auth(self._page)

    async def _async_setup_cdp_proxy_auth(self, page) -> None:
        """Async version of CDP proxy auth setup."""
        cdp = await page.context.new_cdp_session(page)
        self._cdp_session = cdp

        username = self._proxy_username
        password = self._proxy_password

        async def on_auth_required(event: dict) -> None:
            challenge = event.get("authChallenge", {})
            request_id = event.get("requestId")
            if challenge.get("source") == "Proxy":
                log.debug("CDP: proxy auth challenge for %s", request_id)
                await cdp.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": username,
                            "password": password,
                        },
                    },
                )
            else:
                log.debug("CDP: non-proxy auth challenge, cancelling: %s", challenge)
                await cdp.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {"response": "CancelAuth"},
                    },
                )

        async def on_request_paused(event: dict) -> None:
            request_id = event.get("requestId")
            await cdp.send("Fetch.continueRequest", {"requestId": request_id})

        cdp.on("Fetch.authRequired", on_auth_required)
        cdp.on("Fetch.requestPaused", on_request_paused)

        await cdp.send(
            "Fetch.enable",
            {
                "handleAuthRequests": True,
                "patterns": [{"requestStage": "Response"}],
            },
        )
        log.info("CDP Fetch proxy auth handler installed.")

    def _get_proxy_and_launch_args(self) -> tuple:
        """Extract proxy config and build Chrome launch args.

        Returns (proxy_url, launch_args).  Sets self._proxy_username/password
        as a side-effect if a proxy is configured.
        """
        proxy_url = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )

        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-blink-features=AutomationControlled",
        ]

        if proxy_url:
            parsed = urlparse(proxy_url)
            server = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port:
                server += f":{parsed.port}"
            launch_args.append(f"--proxy-server={server}")
            self._proxy_username = parsed.username
            self._proxy_password = parsed.password
            log.info(
                "Proxy configured: server=%s, authenticated=%s",
                server,
                bool(parsed.username),
            )

        return proxy_url, launch_args

    def _launch_browser(self) -> None:
        """Sync browser setup (shared between sync init path)."""
        proxy_url, launch_args = self._get_proxy_and_launch_args()

        self._browser = self._pw.chromium.launch(
            headless=True,
            channel="chrome",
            args=launch_args,
        )

        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 960},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )

        self._context.set_default_timeout(30_000)
        self._context.set_default_navigation_timeout(30_000)

        self._page = self._context.new_page()

        self._page.on("dialog", lambda d: d.dismiss())

        if self._proxy_username:
            self._setup_cdp_proxy_auth(self._page)

    def _maybe_await(self, obj):
        """If in async mode, run a coroutine on the private event loop.

        In sync mode, *obj* is already a concrete value (Playwright sync API
        returns plain objects), so just return it.  In async mode, *obj* is a
        coroutine that must be awaited.
        """
        if self._async_mode and asyncio.iscoroutine(obj):
            return self._async_loop.run_until_complete(obj)
        return obj

    def _setup_cdp_proxy_auth(self, page) -> None:
        """Set up CDP Fetch domain to handle proxy 407 auth challenges.

        Instead of relying on Playwright's proxy={username, password} (which
        causes Chromium to hang on certain sites like google.com), we use the
        CDP Fetch domain to intercept 407 challenges and respond with
        credentials programmatically.
        """
        cdp = self._maybe_await(page.context.new_cdp_session(page))
        self._cdp_session = cdp

        username = self._proxy_username
        password = self._proxy_password
        maybe_await = self._maybe_await

        def on_auth_required(event: dict) -> None:
            challenge = event.get("authChallenge", {})
            request_id = event.get("requestId")
            if challenge.get("source") == "Proxy":
                log.debug("CDP: proxy auth challenge for %s", request_id)
                maybe_await(cdp.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": username,
                            "password": password,
                        },
                    },
                ))
            else:
                # Not a proxy challenge — cancel so the browser handles it
                log.debug("CDP: non-proxy auth challenge, cancelling: %s", challenge)
                maybe_await(cdp.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {"response": "CancelAuth"},
                    },
                ))

        def on_request_paused(event: dict) -> None:
            request_id = event.get("requestId")
            maybe_await(cdp.send("Fetch.continueRequest", {"requestId": request_id}))

        cdp.on("Fetch.authRequired", on_auth_required)
        cdp.on("Fetch.requestPaused", on_request_paused)

        self._maybe_await(cdp.send(
            "Fetch.enable",
            {
                "handleAuthRequests": True,
                "patterns": [{"requestStage": "Response"}],
            },
        ))
        log.info("CDP Fetch proxy auth handler installed.")

    @property
    def page(self):
        return self._ensure_browser()

    @property
    def element_map(self) -> dict[int, dict]:
        return self._element_map

    # ----- snapshot ----------------------------------------------------------

    def take_snapshot(self, max_text_length: int = 5000) -> str:
        """Build a text representation of the current page state."""
        page = self.page

        url = page.url
        title = self._maybe_await(page.title())

        # Collect interactive elements
        elements = self._collect_interactive_elements()
        self._element_map = {e["index"]: e for e in elements}

        lines: list[str] = []
        lines.append(f"URL: {url}")
        lines.append(f"Title: {title}")
        lines.append("")
        lines.append("=== Interactive Elements ===")

        if not elements:
            lines.append("(no interactive elements found)")
        else:
            for el in elements:
                lines.append(self._format_element(el))

        lines.append("")
        lines.append("=== Page Text ===")

        try:
            text = self._maybe_await(page.inner_text("body")) or ""
        except Exception:
            text = ""

        text = text.strip()
        if len(text) > max_text_length:
            text = text[:max_text_length] + f"\n... (truncated at {max_text_length} chars)"

        # Collapse runs of whitespace / blank lines for readability
        collapsed: list[str] = []
        prev_blank = False
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                if not prev_blank:
                    collapsed.append("")
                prev_blank = True
            else:
                collapsed.append(stripped)
                prev_blank = False
        lines.append("\n".join(collapsed))

        return "\n".join(lines)

    def _collect_interactive_elements(self) -> list[dict]:
        """Query the DOM for interactive elements and return structured info."""
        page = self.page

        # JavaScript that runs in the browser to enumerate interactive elements.
        # Returns a JSON-serialisable list.
        js = """
        () => {
            const seen = new Set();
            const results = [];
            const selectors = [
                'a[href]',
                'button',
                'input:not([type="hidden"])',
                'textarea',
                'select',
                '[role="button"]',
                '[role="link"]',
                '[role="tab"]',
                '[role="menuitem"]',
                '[role="checkbox"]',
                '[role="radio"]',
                '[onclick]',
                '[tabindex]:not([tabindex="-1"])',
            ];

            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    if (seen.has(el)) continue;
                    seen.add(el);

                    // Skip invisible elements
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;

                    const tag = el.tagName.toLowerCase();
                    const type = el.getAttribute('type') || '';
                    const name = el.getAttribute('name') || '';
                    const id = el.getAttribute('id') || '';
                    const placeholder = el.getAttribute('placeholder') || '';
                    const href = el.getAttribute('href') || '';
                    const role = el.getAttribute('role') || '';
                    const ariaLabel = el.getAttribute('aria-label') || '';
                    const value = el.value !== undefined ? String(el.value) : '';

                    // Visible text — keep it short
                    let text = (el.innerText || el.textContent || '').trim();
                    if (text.length > 80) text = text.substring(0, 77) + '...';

                    // For selects, collect option labels
                    let options = [];
                    if (tag === 'select') {
                        options = Array.from(el.options).map(o => ({
                            value: o.value,
                            label: o.text.trim(),
                            selected: o.selected,
                        }));
                    }

                    // Checked state for checkboxes/radios
                    const checked = (type === 'checkbox' || type === 'radio') ? el.checked : null;

                    // Build a unique CSS selector for this element
                    let cssSelector = '';
                    if (id) {
                        cssSelector = '#' + CSS.escape(id);
                    } else if (name) {
                        cssSelector = tag + '[name="' + name.replace(/"/g, '\\\\"') + '"]';
                    }

                    results.push({
                        tag, type, name, id, placeholder, href, role,
                        ariaLabel, value, text, options, checked, cssSelector,
                        top: rect.top,
                    });
                }
            }

            // Sort by vertical position on page
            results.sort((a, b) => a.top - b.top);

            return results;
        }
        """

        try:
            raw = self._maybe_await(page.evaluate(js))
        except Exception as exc:
            log.warning("Failed to collect interactive elements: %s", exc)
            return []

        elements: list[dict] = []
        for i, el in enumerate(raw, start=1):
            el["index"] = i
            elements.append(el)

        return elements

    def _format_element(self, el: dict) -> str:
        """Format a single element for the snapshot output."""
        idx = el["index"]
        tag = el["tag"]
        etype = el.get("type", "")
        text = el.get("text", "")
        href = el.get("href", "")
        name = el.get("name", "")
        placeholder = el.get("placeholder", "")
        value = el.get("value", "")
        role = el.get("role", "")
        aria = el.get("ariaLabel", "")
        options = el.get("options", [])
        checked = el.get("checked")

        # Determine element description
        if tag == "a":
            label = text or aria or href
            desc = f'link "{label}"'
            if href:
                desc += f" href=\"{href}\""

        elif tag == "button" or role == "button":
            label = text or aria or "(unnamed button)"
            desc = f'button "{label}"'

        elif tag == "input":
            type_str = etype or "text"
            desc = f"input[{type_str}]"
            if name:
                desc += f' name="{name}"'
            if value:
                desc += f' value="{value}"'
            if placeholder:
                desc += f' placeholder="{placeholder}"'
            if checked is not None:
                desc += " checked" if checked else " unchecked"

        elif tag == "textarea":
            desc = "textarea"
            if name:
                desc += f' name="{name}"'
            if value:
                short_val = value[:40] + "..." if len(value) > 40 else value
                desc += f' value="{short_val}"'
            if placeholder:
                desc += f' placeholder="{placeholder}"'

        elif tag == "select":
            desc = "select"
            if name:
                desc += f' name="{name}"'
            if value:
                desc += f' value="{value}"'
            if options:
                opt_labels = [o["label"] for o in options[:6]]
                if len(options) > 6:
                    opt_labels.append(f"... +{len(options) - 6} more")
                desc += f' options={json.dumps(opt_labels)}'

        else:
            label = text or aria or role or tag
            desc = f'{tag} "{label}"'

        return f"[{idx}] {desc}"

    # ----- element resolution -----------------------------------------------

    def resolve_element(self, element_index: int | None = None,
                        selector: str | None = None,
                        text: str | None = None) -> Any:
        """Resolve an element from index, selector, or text match."""
        page = self.page

        if element_index is not None:
            el_info = self._element_map.get(element_index)
            if not el_info:
                raise ValueError(
                    f"Element index {element_index} not found. "
                    f"Valid indices: 1-{len(self._element_map)}. "
                    "Call get_page_snapshot to refresh the element list."
                )
            # Use the CSS selector if we have one, otherwise reconstruct
            css = el_info.get("cssSelector")
            if css:
                locator = page.locator(css).first
            else:
                # Fall back to nth-of-type approach
                tag = el_info["tag"]
                idx_in_page = element_index  # approximate
                locator = page.locator(tag).nth(idx_in_page - 1)

            return locator

        if selector is not None:
            return page.locator(selector).first

        if text is not None:
            # Try common patterns: links, buttons, then any visible text
            for role in ["link", "button"]:
                loc = page.get_by_role(role, name=text)
                if self._maybe_await(loc.count()) > 0:
                    return loc.first
            # Fall back to text match
            return page.get_by_text(text, exact=False).first

        raise ValueError(
            "Provide at least one of: element_index, selector, or text."
        )

    # ----- cleanup -----------------------------------------------------------

    def close(self) -> None:
        log.info("Closing browser...")
        try:
            if self._cdp_session:
                self._maybe_await(self._cdp_session.detach())
        except Exception:
            pass
        try:
            if self._context:
                self._maybe_await(self._context.close())
        except Exception:
            pass
        try:
            if self._browser:
                self._maybe_await(self._browser.close())
        except Exception:
            pass
        try:
            if self._pw:
                self._maybe_await(self._pw.stop())
        except Exception:
            pass
        if self._async_loop:
            self._async_loop.close()
        log.info("Browser closed.")


# ---------------------------------------------------------------------------
# Tool handlers
#
# Each handler is a plain synchronous function.  Playwright calls are
# dispatched to the dedicated Playwright thread via run_in_pw_thread().
# ---------------------------------------------------------------------------

browser_mgr = BrowserManager()
session_state = SessionState()

# Shorthand for browser_mgr._maybe_await — keeps handler code concise.
_ma = browser_mgr._maybe_await


def handle_navigate(args: dict) -> str:
    url = args.get("url")
    if not url:
        return json.dumps({"error": "url is required"})

    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def _do():
        page = browser_mgr.page
        try:
            _ma(page.goto(url, wait_until="domcontentloaded"))
        except PlaywrightTimeout:
            return json.dumps({
                "error": f"Timed out loading {url}. The page may still be loading.",
                "partial_snapshot": browser_mgr.take_snapshot(max_text_length=2000),
            })
        except Exception as exc:
            log.warning("Navigation to %s failed: %s", url, exc)
            return json.dumps({"error": f"Failed to navigate to {url}: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": browser_mgr.take_snapshot(),
        })

    return run_in_pw_thread(_do)


def handle_get_page_snapshot(args: dict) -> str:
    max_len = args.get("max_text_length", 5000)

    def _do():
        return json.dumps({
            "snapshot": browser_mgr.take_snapshot(max_text_length=max_len),
        })

    return run_in_pw_thread(_do)


def handle_click(args: dict) -> str:
    def _do():
        try:
            locator = browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
                text=args.get("text"),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            _ma(locator.click(timeout=10_000))
            # Wait briefly for potential navigation or DOM changes
            _ma(browser_mgr.page.wait_for_load_state("domcontentloaded", timeout=5_000))
        except PlaywrightTimeout:
            pass  # Page may not navigate — that's fine
        except Exception as exc:
            return json.dumps({
                "error": f"Click failed: {exc}",
                "snapshot": browser_mgr.take_snapshot(max_text_length=2000),
            })

        return json.dumps({
            "status": "ok",
            "snapshot": browser_mgr.take_snapshot(),
        })

    return run_in_pw_thread(_do)


def handle_fill(args: dict) -> str:
    value = args.get("value")
    if value is None:
        return json.dumps({"error": "value is required"})

    clear_first = args.get("clear_first", True)

    def _do():
        try:
            locator = browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            if clear_first:
                _ma(locator.fill(value, timeout=10_000))
            else:
                _ma(locator.press_sequentially(value, delay=50, timeout=10_000))
        except Exception as exc:
            return json.dumps({"error": f"Fill failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "filled": value if len(value) <= 50 else value[:47] + "...",
        })

    return run_in_pw_thread(_do)


def handle_select_option(args: dict) -> str:
    def _do():
        try:
            locator = browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        value = args.get("value")
        label = args.get("label")

        try:
            if label:
                _ma(locator.select_option(label=label, timeout=10_000))
            elif value:
                _ma(locator.select_option(value=value, timeout=10_000))
            else:
                return json.dumps({"error": "Provide either 'value' or 'label'."})
        except Exception as exc:
            return json.dumps({"error": f"Select failed: {exc}"})

        return json.dumps({"status": "ok"})

    return run_in_pw_thread(_do)


def handle_press_key(args: dict) -> str:
    key = args.get("key")
    if not key:
        return json.dumps({"error": "key is required"})

    def _do():
        page = browser_mgr.page

        # Optionally focus an element first
        el_index = args.get("element_index")
        selector = args.get("selector")
        if el_index is not None or selector is not None:
            try:
                locator = browser_mgr.resolve_element(
                    element_index=el_index,
                    selector=selector,
                )
                _ma(locator.focus(timeout=5_000))
            except Exception as exc:
                return json.dumps({"error": f"Could not focus element: {exc}"})

        try:
            _ma(page.keyboard.press(key))
            # Brief wait for any navigation or DOM update
            try:
                _ma(page.wait_for_load_state("domcontentloaded", timeout=3_000))
            except PlaywrightTimeout:
                pass
        except Exception as exc:
            return json.dumps({"error": f"Key press failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": browser_mgr.take_snapshot(),
        })

    return run_in_pw_thread(_do)


def handle_scroll(args: dict) -> str:
    direction = args.get("direction", "down")
    amount = args.get("amount", "page")

    def _do():
        page = browser_mgr.page

        if amount == "page":
            pixels = 960  # match viewport height
        else:
            try:
                pixels = int(amount)
            except (ValueError, TypeError):
                pixels = 960

        if direction == "up":
            pixels = -pixels

        try:
            _ma(page.evaluate(f"window.scrollBy(0, {pixels})"))
            _ma(page.wait_for_timeout(500))  # let lazy-loaded content appear
        except Exception as exc:
            return json.dumps({"error": f"Scroll failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": browser_mgr.take_snapshot(),
        })

    return run_in_pw_thread(_do)


def handle_go_back(args: dict) -> str:
    def _do():
        page = browser_mgr.page
        try:
            _ma(page.go_back(wait_until="domcontentloaded", timeout=15_000))
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            return json.dumps({"error": f"Go back failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": browser_mgr.take_snapshot(),
        })

    return run_in_pw_thread(_do)


def handle_wait(args: dict) -> str:
    selector = args.get("selector")
    if not selector:
        return json.dumps({"error": "selector is required"})

    timeout_sec = args.get("timeout_seconds", 10)

    def _do():
        page = browser_mgr.page

        try:
            _ma(page.wait_for_selector(selector, timeout=timeout_sec * 1000))
            return json.dumps({
                "status": "found",
                "selector": selector,
            })
        except PlaywrightTimeout:
            return json.dumps({
                "status": "timeout",
                "message": f"Element '{selector}' did not appear within {timeout_sec}s.",
            })
        except Exception as exc:
            return json.dumps({"error": f"Wait failed: {exc}"})

    return run_in_pw_thread(_do)


def handle_execute_javascript(args: dict) -> str:
    script = args.get("script")
    if not script:
        return json.dumps({"error": "script is required"})

    def _do():
        page = browser_mgr.page
        try:
            result = _ma(page.evaluate(script))
            return json.dumps({
                "status": "ok",
                "result": result,
            })
        except Exception as exc:
            return json.dumps({"error": f"JavaScript execution failed: {exc}"})

    return run_in_pw_thread(_do)


def handle_save_state(args: dict) -> str:
    key = args.get("key")
    value = args.get("value")
    if key is None:
        return json.dumps({"error": "key is required"})
    if value is None:
        return json.dumps({"error": "value is required"})

    session_state.set(key, value)
    return json.dumps({
        "status": "ok",
        "key": key,
        "total_keys": session_state.size(),
    })


def handle_load_state(args: dict) -> str:
    return json.dumps({
        "state": session_state.get_all(),
    })


# Dispatch table
TOOL_HANDLERS = {
    "navigate": handle_navigate,
    "get_page_snapshot": handle_get_page_snapshot,
    "click": handle_click,
    "fill": handle_fill,
    "select_option": handle_select_option,
    "press_key": handle_press_key,
    "scroll": handle_scroll,
    "go_back": handle_go_back,
    "wait": handle_wait,
    "execute_javascript": handle_execute_javascript,
    "save_state": handle_save_state,
    "load_state": handle_load_state,
}

# Maps tool name -> primary required parameter name, used to recover when the
# orchestrator sends a bare value instead of a JSON object (e.g. when Bedrock
# streaming fails to parse tool args and falls back to a raw string).
TOOL_PRIMARY_PARAM: dict[str, str] = {
    "navigate": "url",
    "press_key": "key",
    "scroll": "direction",
    "wait": "selector",
    "execute_javascript": "script",
    "fill": "value",
}


# ---------------------------------------------------------------------------
# gRPC servicer
# ---------------------------------------------------------------------------

class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):

    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True, message="ok")

    def Invoke(self, request, context):
        tool = request.tool_name
        log.info("Invoke: %s", tool)

        handler = TOOL_HANDLERS.get(tool)
        if not handler:
            return capability_pb2.InvokeResponse(
                error=f"Unknown tool: {tool}. Available: {', '.join(TOOL_HANDLERS.keys())}"
            )

        try:
            args = json.loads(request.args_json) if request.args_json else {}
        except json.JSONDecodeError as exc:
            # args_json was not valid JSON at all — try to use the raw
            # bytes as a string value for the tool's primary parameter.
            raw = request.args_json.decode("utf-8", errors="replace") if isinstance(request.args_json, bytes) else str(request.args_json)
            primary = TOOL_PRIMARY_PARAM.get(tool)
            if primary:
                log.warning("args_json was not valid JSON for tool %s, treating raw value as '%s'", tool, primary)
                args = {primary: raw}
            else:
                return capability_pb2.InvokeResponse(
                    error=f"Invalid JSON arguments: {exc}"
                )

        # Handle the case where args_json was valid JSON but decoded to a
        # non-object type (e.g. a bare string like '"https://example.com"').
        # This happens when the orchestrator's streaming parser fails and
        # falls back to sending the raw string from the LLM.
        if not isinstance(args, dict):
            primary = TOOL_PRIMARY_PARAM.get(tool)
            if primary:
                log.warning("args for tool %s was %s instead of dict, wrapping as {'%s': ...}", tool, type(args).__name__, primary)
                args = {primary: args}
            else:
                return capability_pb2.InvokeResponse(
                    error=f"Expected JSON object for tool arguments, got {type(args).__name__}"
                )

        try:
            result = handler(args)
        except Exception as exc:
            log.exception("Tool %s raised an exception", tool)
            return capability_pb2.InvokeResponse(
                error=f"Tool '{tool}' failed: {exc}"
            )

        return capability_pb2.InvokeResponse(result_json=result.encode("utf-8"))

    def StreamInvoke(self, request, context):
        """Wrap synchronous Invoke as a single-chunk stream."""
        resp = self.Invoke(request, context)
        yield capability_pb2.InvokeChunk(
            data=resp.result_json,
            done=True,
            error=resp.error,
        )


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def serve() -> None:
    port = int(os.environ.get("PORT", "50051"))

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    capability_pb2_grpc.add_CapabilityServicer_to_server(
        CapabilityServicer(), server
    )
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    log.info("gRPC server listening on :%d", port)

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    stop_event.wait()

    log.info("Stopping gRPC server...")
    server.stop(grace=5)
    run_in_pw_thread(browser_mgr.close)
    log.info("Server stopped.")


if __name__ == "__main__":
    serve()
