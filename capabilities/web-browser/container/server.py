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
from typing import Any
from urllib.parse import urlparse

import grpc
import capability_pb2
import capability_pb2_grpc

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("web-browser")

# ---------------------------------------------------------------------------
# Dedicated asyncio event loop — runs on a daemon thread so that async
# Playwright calls can be dispatched from synchronous gRPC handler threads.
# ---------------------------------------------------------------------------

_async_loop = asyncio.new_event_loop()


def _start_async_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


_async_thread = threading.Thread(target=_start_async_loop, args=(_async_loop,), daemon=True)
_async_thread.start()


def _run_async(coro):
    """Submit a coroutine to the dedicated event loop and block until it completes."""
    future = asyncio.run_coroutine_threadsafe(coro, _async_loop)
    return future.result()


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

    All Playwright operations are async and run on a dedicated event loop
    thread (_async_loop).  Synchronous callers use _run_async() to bridge.
    """

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp_session = None
        self._proxy_username: str | None = None
        self._proxy_password: str | None = None
        self._element_map: dict[int, dict] = {}
        self._init_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Page:
        """Lazily start the browser on first use."""
        if self._page is not None:
            return self._page

        async with self._init_lock:
            if self._page is not None:
                return self._page

            log.info("Starting Playwright and launching Chrome...")

            self._pw = await async_playwright().start()

            # Respect egress proxy injected by the orchestrator.
            # The URL contains credentials (e.g. http://selu:token@host:port).
            #
            # IMPORTANT: We do NOT use Playwright's proxy={username, password}
            # parameter.  When credentials are passed that way, Playwright
            # configures Chromium via CDP in a manner that causes certain
            # sites (e.g. google.com) to hang — Chromium opens the CONNECT
            # tunnel but never sends any data through it.
            #
            # Instead, we pass --proxy-server as a Chromium flag and handle
            # the 407 Proxy Authentication challenge ourselves via the CDP
            # Fetch domain (Fetch.authRequired event).  This is the same
            # mechanism Chrome DevTools uses and works reliably with all sites.
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
                # Reconstruct server URL without embedded credentials
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

            # Reasonable defaults for page loads
            self._context.set_default_timeout(30_000)
            self._context.set_default_navigation_timeout(30_000)

            self._page = await self._context.new_page()

            # Dismiss simple dialogs automatically
            async def _dismiss_dialog(d):
                await d.dismiss()
            self._page.on("dialog", _dismiss_dialog)

            # Set up CDP Fetch domain for proxy authentication.
            # Chromium follows RFC 7235: it sends CONNECT without credentials,
            # expects a 407 with Proxy-Authenticate, and retries with creds.
            # We intercept the 407 challenge via CDP and supply credentials.
            if self._proxy_username:
                await self._setup_cdp_proxy_auth(self._page)

            log.info("Browser ready.")
            return self._page

    async def _setup_cdp_proxy_auth(self, page: Page) -> None:
        """Set up CDP Fetch domain to handle proxy 407 auth challenges.

        Instead of relying on Playwright's proxy={username, password} (which
        causes Chromium to hang on certain sites like google.com), we use the
        CDP Fetch domain to intercept 407 challenges and respond with
        credentials programmatically.
        """
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
                # Not a proxy challenge — cancel so the browser handles it
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

    async def get_page(self) -> Page:
        """Get the current page, launching the browser if needed."""
        return await self._ensure_browser()

    @property
    def element_map(self) -> dict[int, dict]:
        return self._element_map

    # ----- snapshot ----------------------------------------------------------

    async def take_snapshot(self, max_text_length: int = 5000) -> str:
        """Build a text representation of the current page state."""
        page = await self.get_page()

        url = page.url
        title = await page.title()

        # Collect interactive elements
        elements = await self._collect_interactive_elements()
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
            text = await page.inner_text("body") or ""
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

    async def _collect_interactive_elements(self) -> list[dict]:
        """Query the DOM for interactive elements and return structured info."""
        page = await self.get_page()

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
            raw = await page.evaluate(js)
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

    async def resolve_element(self, element_index: int | None = None,
                              selector: str | None = None,
                              text: str | None = None) -> Any:
        """Resolve an element from index, selector, or text match."""
        page = await self.get_page()

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
                if await loc.count() > 0:
                    return loc.first
            # Fall back to text match
            return page.get_by_text(text, exact=False).first

        raise ValueError(
            "Provide at least one of: element_index, selector, or text."
        )

    # ----- cleanup -----------------------------------------------------------

    async def close(self) -> None:
        log.info("Closing browser...")
        try:
            if self._cdp_session:
                await self._cdp_session.detach()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        log.info("Browser closed.")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

browser_mgr = BrowserManager()
session_state = SessionState()


def handle_navigate(args: dict) -> str:
    url = args.get("url")
    if not url:
        return json.dumps({"error": "url is required"})

    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def _navigate():
        page = await browser_mgr.get_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            return json.dumps({
                "error": f"Timed out loading {url}. The page may still be loading.",
                "partial_snapshot": await browser_mgr.take_snapshot(max_text_length=2000),
            })
        except Exception as exc:
            log.warning("Navigation to %s failed: %s", url, exc)
            return json.dumps({"error": f"Failed to navigate to {url}: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(),
        })

    return _run_async(_navigate())


def handle_get_page_snapshot(args: dict) -> str:
    max_len = args.get("max_text_length", 5000)

    async def _snapshot():
        return json.dumps({
            "snapshot": await browser_mgr.take_snapshot(max_text_length=max_len),
        })

    return _run_async(_snapshot())


def handle_click(args: dict) -> str:
    async def _click():
        try:
            locator = await browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
                text=args.get("text"),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            await locator.click(timeout=10_000)
            # Wait briefly for potential navigation or DOM changes
            page = await browser_mgr.get_page()
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeout:
            pass  # Page may not navigate — that's fine
        except Exception as exc:
            return json.dumps({
                "error": f"Click failed: {exc}",
                "snapshot": await browser_mgr.take_snapshot(max_text_length=2000),
            })

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(),
        })

    return _run_async(_click())


def handle_fill(args: dict) -> str:
    value = args.get("value")
    if value is None:
        return json.dumps({"error": "value is required"})

    clear_first = args.get("clear_first", True)

    async def _fill():
        try:
            locator = await browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            if clear_first:
                await locator.fill(value, timeout=10_000)
            else:
                await locator.press_sequentially(value, delay=50, timeout=10_000)
        except Exception as exc:
            return json.dumps({"error": f"Fill failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "filled": value if len(value) <= 50 else value[:47] + "...",
        })

    return _run_async(_fill())


def handle_select_option(args: dict) -> str:
    async def _select():
        try:
            locator = await browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        value = args.get("value")
        label = args.get("label")

        try:
            if label:
                await locator.select_option(label=label, timeout=10_000)
            elif value:
                await locator.select_option(value=value, timeout=10_000)
            else:
                return json.dumps({"error": "Provide either 'value' or 'label'."})
        except Exception as exc:
            return json.dumps({"error": f"Select failed: {exc}"})

        return json.dumps({"status": "ok"})

    return _run_async(_select())


def handle_press_key(args: dict) -> str:
    key = args.get("key")
    if not key:
        return json.dumps({"error": "key is required"})

    async def _press_key():
        page = await browser_mgr.get_page()

        # Optionally focus an element first
        el_index = args.get("element_index")
        selector = args.get("selector")
        if el_index is not None or selector is not None:
            try:
                locator = await browser_mgr.resolve_element(
                    element_index=el_index,
                    selector=selector,
                )
                await locator.focus(timeout=5_000)
            except Exception as exc:
                return json.dumps({"error": f"Could not focus element: {exc}"})

        try:
            await page.keyboard.press(key)
            # Brief wait for any navigation or DOM update
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3_000)
            except PlaywrightTimeout:
                pass
        except Exception as exc:
            return json.dumps({"error": f"Key press failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(),
        })

    return _run_async(_press_key())


def handle_scroll(args: dict) -> str:
    direction = args.get("direction", "down")
    amount = args.get("amount", "page")

    async def _scroll():
        page = await browser_mgr.get_page()

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
            await page.evaluate(f"window.scrollBy(0, {pixels})")
            await page.wait_for_timeout(500)  # let lazy-loaded content appear
        except Exception as exc:
            return json.dumps({"error": f"Scroll failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(),
        })

    return _run_async(_scroll())


def handle_go_back(args: dict) -> str:
    async def _go_back():
        page = await browser_mgr.get_page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            return json.dumps({"error": f"Go back failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(),
        })

    return _run_async(_go_back())


def handle_wait(args: dict) -> str:
    selector = args.get("selector")
    if not selector:
        return json.dumps({"error": "selector is required"})

    timeout_sec = args.get("timeout_seconds", 10)

    async def _wait():
        page = await browser_mgr.get_page()
        try:
            await page.wait_for_selector(selector, timeout=timeout_sec * 1000)
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

    return _run_async(_wait())


def handle_execute_javascript(args: dict) -> str:
    script = args.get("script")
    if not script:
        return json.dumps({"error": "script is required"})

    async def _exec_js():
        page = await browser_mgr.get_page()
        try:
            result = await page.evaluate(script)
            return json.dumps({
                "status": "ok",
                "result": result,
            })
        except Exception as exc:
            return json.dumps({"error": f"JavaScript execution failed: {exc}"})

    return _run_async(_exec_js())


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
    _run_async(browser_mgr.close())
    log.info("Server stopped.")


if __name__ == "__main__":
    serve()
