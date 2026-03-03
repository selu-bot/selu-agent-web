"""
Selu Web Browser Capability — gRPC server with Playwright-based browser automation.

Maintains a persistent browser instance across Invoke calls within a
session.  Each tool call returns structured text that the LLM can reason about.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import shutil
import subprocess
import threading
from concurrent import futures
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import grpc
import capability_pb2
import capability_pb2_grpc

from playwright.async_api import (
    async_playwright,
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Page as AsyncPage,
    Playwright as AsyncPlaywright,
    TimeoutError as PlaywrightTimeout,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("web-browser")

# ---------------------------------------------------------------------------
# Playwright thread — a dedicated thread running its own asyncio event loop.
#
# All Playwright operations run as coroutines on this loop.  External threads
# (gRPC workers) submit work via run_in_pw_thread() which uses
# asyncio.run_coroutine_threadsafe().
#
# This avoids the fundamental conflict between grpcio (which may install a
# C-level asyncio running-loop that leaks into worker threads) and
# Playwright's sync API (which refuses to start when *any* running loop is
# detected).  By owning the event loop ourselves and using the async
# Playwright API exclusively, there is no conflict.
# ---------------------------------------------------------------------------

_pw_loop: asyncio.AbstractEventLoop | None = None
_pw_thread_started = threading.Event()


def _pw_thread_main() -> None:
    """Entry point for the Playwright worker thread."""
    global _pw_loop
    _pw_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_pw_loop)
    _pw_thread_started.set()
    log.info("Playwright worker thread started (asyncio event loop).")
    _pw_loop.run_forever()
    log.info("Playwright worker thread exiting.")


_pw_thread = threading.Thread(target=_pw_thread_main, daemon=True)
_pw_thread.start()
_pw_thread_started.wait()


def run_in_pw_thread(fn: Callable, timeout: float = 120) -> Any:
    """Execute *fn* on the Playwright thread and return its result.

    *fn* may be an async function, a coroutine, or a plain callable.
    Blocks the calling thread until completion or *timeout* seconds elapse.
    If *fn* raises, the exception is re-raised in the caller.
    If the timeout fires, the future is cancelled and a TimeoutError is raised
    so the gRPC worker thread is freed.
    """
    if asyncio.iscoroutinefunction(fn):
        coro = fn()
    elif asyncio.iscoroutine(fn):
        coro = fn
    else:
        async def _wrap():
            return fn()
        coro = _wrap()
    future = asyncio.run_coroutine_threadsafe(coro, _pw_loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise TimeoutError(
            f"Playwright operation timed out after {timeout}s"
        )


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
# Multiple threads share the browser context but each gets its own page (tab).
# ---------------------------------------------------------------------------

class BrowserManager:
    """Manages a single Playwright browser instance with per-thread pages.

    All async methods run on the Playwright worker thread's event loop
    (submitted via run_in_pw_thread).

    Each conversation thread gets its own browser tab (page) and element map,
    so multiple threads within the same session don't interfere with each other.
    """

    _DEFAULT_THREAD = "__default__"

    def __init__(self) -> None:
        self._pw: AsyncPlaywright | None = None
        self._browser: AsyncBrowser | None = None
        self._context: AsyncBrowserContext | None = None
        # Per-thread state: each thread gets its own page and element map
        self._pages: dict[str, AsyncPage] = {}
        self._element_maps: dict[str, dict[int, dict]] = {}
        self._browser_log_file = Path(
            os.environ.get("BROWSER_LOG_FILE", "/tmp/browser-debug.log")
        )
        home_dir = Path(os.environ.get("HOME", "/tmp"))
        state_root = Path(
            os.environ.get("BROWSER_STATE_DIR", str(home_dir / ".browser-runtime"))
        )
        self._browser_tmp_dir = Path(
            os.environ.get("BROWSER_TMP_DIR", str(state_root / "tmp"))
        )
        self._browser_user_data_dir = Path(
            os.environ.get("BROWSER_USER_DATA_DIR", str(state_root / "user-data"))
        )
        self._browser_runtime_dir = Path(
            os.environ.get("BROWSER_RUNTIME_DIR", str(state_root / "xdg-runtime"))
        )
        self._browser_crash_dir = Path(
            os.environ.get("BROWSER_CRASH_DIR", str(state_root / "crash"))
        )

    async def ensure_page(self, thread_id: str | None = None) -> AsyncPage:
        """Return the page for the given thread, creating one if needed.

        On first call, starts the browser context. Subsequent calls for the
        same thread reuse the existing page. Different threads get separate tabs.
        """
        tid = thread_id or self._DEFAULT_THREAD

        # Fast path: page already exists and is open
        page = self._pages.get(tid)
        if page is not None and not page.is_closed():
            return page

        # If a page existed but was closed, clean it up
        if page is not None:
            log.warning("Page for thread %s was closed; creating a new one.", tid)
            self._pages.pop(tid, None)
            self._element_maps.pop(tid, None)

        # Ensure the browser context is running
        await self._ensure_context()

        # Reuse the initial page from the persistent context for the first
        # thread, to avoid Firefox "can't open new page" errors.
        if hasattr(self, '_initial_page') and self._initial_page is not None:
            page = self._initial_page
            self._initial_page = None
        else:
            page = await self._context.new_page()

        async def _dismiss(d):
            await d.dismiss()
        page.on("dialog", _dismiss)

        self._pages[tid] = page
        self._element_maps[tid] = {}
        log.info("Created new page for thread %s (total pages: %d).", tid, len(self._pages))
        return page

    async def _ensure_context(self) -> None:
        """Start the browser context if not already running."""
        if self._context is not None:
            return

        self._prepare_browser_runtime_dirs()
        self._log_launch_environment()

        last_exc: Exception | None = None
        for launch in self._candidate_launches():
            label = launch["label"]
            log.info("Starting Playwright and launching %s...", label)
            self._prepare_browser_runtime_dirs()
            self._prepare_browser_log_file()
            try:
                await self._setup(launch=launch)
                log.info("Browser ready (%s).", label)
                return
            except Exception as exc:
                last_exc = exc
                log.exception("Browser setup failed (%s): %s", label, exc)
                self._log_browser_stderr_tail()
                await self._cleanup_runtime()

        raise RuntimeError("Unable to start browser context with Firefox.") from last_exc

    def _candidate_launches(self) -> list[dict[str, str]]:
        """Return Firefox launch candidates in order."""
        launches: list[dict[str, str]] = [
            {"kind": "default", "value": "", "label": "firefox:default"}
        ]

        explicit_exec = os.environ.get("PLAYWRIGHT_FIREFOX_EXECUTABLE", "").strip()
        if explicit_exec and os.path.exists(explicit_exec):
            launches.insert(0, {
                "kind": "executable",
                "value": explicit_exec,
                "label": f"executable:{explicit_exec}",
            })

        return launches

    def _browser_env(self, proxy_configured: bool) -> dict[str, str]:
        env = dict(os.environ)
        env["BROWSER_LOG_FILE"] = str(self._browser_log_file)
        env["TMPDIR"] = str(self._browser_tmp_dir)
        env["XDG_RUNTIME_DIR"] = str(self._browser_runtime_dir)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        env.pop("DBUS_SYSTEM_BUS_ADDRESS", None)
        if proxy_configured:
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
            ):
                env.pop(key, None)
        return env

    async def _setup(self, launch: dict[str, str]) -> None:
        """Full browser setup using the async Playwright API."""
        self._pw = await async_playwright().start()
        log.info("Using Playwright Async API (Firefox).")

        proxy, launch_args = self._get_proxy_and_launch_args()
        launch_options: dict[str, Any] = {
            "headless": True,
            "args": launch_args,
            "env": self._browser_env(proxy_configured=bool(proxy)),
            "viewport": {"width": 1280, "height": 960},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) "
                "Gecko/20100101 Firefox/134.0"
            ),
            "ignore_https_errors": True,
        }
        if launch["kind"] == "executable":
            launch_options["firefox_user_prefs"] = {
                "browser.shell.checkDefaultBrowser": False,
            }
            launch_options["executable_path"] = launch["value"]
        if proxy:
            launch_options["proxy"] = proxy

        # Use a persistent context so profile data lives in an explicit writable
        # directory instead of an implicit temp profile.
        self._context = await self._pw.firefox.launch_persistent_context(
            user_data_dir=str(self._browser_user_data_dir),
            **launch_options,
        )
        self._browser = self._context.browser
        if self._browser is not None:
            if not self._browser.is_connected():
                raise RuntimeError(
                    f"Browser disconnected immediately after launch ({launch['label']})."
                )
            self._browser.on(
                "disconnected",
                lambda: log.warning("Browser process disconnected."),
            )
        else:
            log.warning("Persistent context did not expose browser handle.")

        self._context.set_default_timeout(30_000)
        self._context.set_default_navigation_timeout(30_000)

        # The persistent context may create an initial page automatically.
        # Store it so the first ensure_page() call can reuse it instead of
        # creating a new one (Firefox persistent contexts require at least
        # one page to stay alive).
        pages = self._context.pages
        if pages:
            self._initial_page = pages[0]
            async def _dismiss(d):
                await d.dismiss()
            self._initial_page.on("dialog", _dismiss)
        else:
            self._initial_page = None

    def _get_proxy_and_launch_args(self) -> tuple[dict[str, str] | None, list[str]]:
        """Extract proxy config and build Firefox launch args.

        Returns (proxy, launch_args), where proxy is a Playwright launch config
        dict or None.
        """
        proxy_url = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )

        proxy: dict[str, str] | None = None
        # Firefox uses fewer command-line flags than Chromium.
        # Most hardening/rendering flags are Chromium-specific.
        launch_args: list[str] = []

        if proxy_url:
            parsed = urlparse(proxy_url)
            if parsed.scheme and parsed.hostname:
                server = f"{parsed.scheme}://{parsed.hostname}"
                if parsed.port:
                    server += f":{parsed.port}"
                proxy = {"server": server}
                if parsed.username is not None:
                    proxy["username"] = unquote(parsed.username)
                if parsed.password is not None:
                    proxy["password"] = unquote(parsed.password)
                log.info(
                    "Proxy configured: server=%s, authenticated=%s",
                    server,
                    bool(parsed.username),
                )
            else:
                log.warning(
                    "Ignoring invalid proxy URL from environment: %r", proxy_url
                )

        return proxy, launch_args

    def _prepare_browser_runtime_dirs(self) -> None:
        """Ensure runtime directories used by the browser exist and are writable."""
        fallback_root = Path("/tmp/browser-runtime")
        self._browser_tmp_dir = self._ensure_writable_dir(
            self._browser_tmp_dir, fallback_root / "tmp"
        )
        self._browser_user_data_dir = self._ensure_writable_dir(
            self._browser_user_data_dir, fallback_root / "user-data"
        )
        self._browser_runtime_dir = self._ensure_writable_dir(
            self._browser_runtime_dir, fallback_root / "xdg-runtime"
        )
        self._browser_crash_dir = self._ensure_writable_dir(
            self._browser_crash_dir, fallback_root / "crash"
        )
        self._ensure_writable_dir(self._browser_tmp_dir / "cache", fallback_root / "tmp" / "cache")

    def _ensure_writable_dir(self, preferred: Path, fallback: Path) -> Path:
        for path in (preferred, fallback):
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".write-test"
                probe.write_text("ok")
                probe.unlink(missing_ok=True)
                return path
            except Exception as exc:
                log.warning("Failed to prepare runtime dir %s: %s", path, exc)
        return fallback

    def _prepare_browser_log_file(self) -> None:
        """Ensure the browser log file exists and is empty for this launch attempt."""
        path = self._browser_log_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        except Exception as exc:
            log.warning("Failed to prepare browser log file %s: %s", path, exc)

    def _log_launch_environment(self) -> None:
        """Emit environment details that matter for Firefox startup issues."""
        log.info(
            "Launch environment: uid=%s gid=%s browser=firefox",
            os.getuid(),
            os.getgid(),
        )
        try:
            tmp_free_mb = shutil.disk_usage(self._browser_tmp_dir).free // (1024 * 1024)
            root_free_mb = shutil.disk_usage("/").free // (1024 * 1024)
            log.info(
                "Storage: tmp=%s free=%sMB root_free=%sMB",
                self._browser_tmp_dir,
                tmp_free_mb,
                root_free_mb,
            )
        except Exception as exc:
            log.warning("Could not read disk usage for temp dirs: %s", exc)

        firefox_bins = ["firefox", "firefox-esr"]
        found = [(name, shutil.which(name)) for name in firefox_bins if shutil.which(name)]
        if not found:
            log.info("No Firefox executable found on PATH (Playwright will use its bundled copy).")
            return

        for name, path in found:
            try:
                proc = subprocess.run(
                    [path, "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                version = (proc.stdout or proc.stderr or "").strip()
                log.info("Firefox binary: %s (%s) => %s", name, path, version or "unknown")
            except Exception as exc:
                log.warning("Failed to read Firefox version for %s (%s): %s", name, path, exc)

    def _log_browser_stderr_tail(self) -> None:
        """Log the tail of the browser log file if present."""
        path = self._browser_log_file
        if not path.exists():
            return
        try:
            lines = path.read_text(errors="replace").splitlines()
            if not lines:
                return
            tail = "\n".join(lines[-40:])
            log.warning("Browser log tail from %s:\n%s", path, tail)
        except Exception as exc:
            log.warning("Failed to read browser log file %s: %s", path, exc)

    @property
    def element_map(self) -> dict[int, dict]:
        """Return the element map for the default thread (backward compat)."""
        return self._element_maps.get(self._DEFAULT_THREAD, {})

    def element_map_for(self, thread_id: str | None = None) -> dict[int, dict]:
        """Return the element map for the given thread."""
        tid = thread_id or self._DEFAULT_THREAD
        return self._element_maps.get(tid, {})

    # ----- snapshot ----------------------------------------------------------

    async def take_snapshot(self, thread_id: str | None = None,
                            max_text_length: int = 5000) -> str:
        """Build a text representation of the current page state."""
        tid = thread_id or self._DEFAULT_THREAD
        page = await self.ensure_page(tid)

        url = page.url
        title = await page.title()

        # Collect interactive elements
        elements = await self._collect_interactive_elements(tid)
        self._element_maps[tid] = {e["index"]: e for e in elements}

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

    async def _collect_interactive_elements(self, thread_id: str | None = None) -> list[dict]:
        """Query the DOM for interactive elements and return structured info."""
        page = await self.ensure_page(thread_id)

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
                              text: str | None = None,
                              thread_id: str | None = None) -> Any:
        """Resolve an element from index, selector, or text match."""
        tid = thread_id or self._DEFAULT_THREAD
        page = await self.ensure_page(tid)

        if element_index is not None:
            el_map = self._element_maps.get(tid, {})
            el_info = el_map.get(element_index)
            if not el_info:
                raise ValueError(
                    f"Element index {element_index} not found. "
                    f"Valid indices: 1-{len(el_map)}. "
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

    async def _cleanup_runtime(self) -> None:
        """Best-effort close of all Playwright resources, resets handles."""
        for tid, page in list(self._pages.items()):
            try:
                if page and not page.is_closed():
                    await page.close()
            except Exception:
                pass
        self._pages.clear()
        self._element_maps.clear()

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

        self._context = None
        self._browser = None
        self._pw = None

    async def close(self) -> None:
        log.info("Closing browser...")
        await self._cleanup_runtime()
        log.info("Browser closed.")


# ---------------------------------------------------------------------------
# Tool handlers
#
# Each handler is a plain synchronous function that returns a JSON string.
# Playwright calls are dispatched to the dedicated Playwright thread as
# async closures via run_in_pw_thread().
# ---------------------------------------------------------------------------

browser_mgr = BrowserManager()
session_state = SessionState()


def handle_navigate(args: dict, thread_id: str) -> str:
    url = args.get("url")
    if not url:
        return json.dumps({"error": "url is required"})

    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def _do():
        try:
            page = await browser_mgr.ensure_page(thread_id)
        except Exception as exc:
            log.exception("Browser startup failed during navigate")
            return json.dumps({"error": f"Failed to start browser: {exc}"})
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            return json.dumps({
                "error": f"Timed out loading {url}. The page may still be loading.",
                "partial_snapshot": await browser_mgr.take_snapshot(thread_id=thread_id, max_text_length=2000),
            })
        except Exception as exc:
            log.warning("Navigation to %s failed: %s", url, exc)
            return json.dumps({"error": f"Failed to navigate to {url}: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(thread_id=thread_id),
        })

    return run_in_pw_thread(_do)


def handle_get_page_snapshot(args: dict, thread_id: str) -> str:
    max_len = args.get("max_text_length", 5000)

    async def _do():
        try:
            snapshot = await browser_mgr.take_snapshot(thread_id=thread_id, max_text_length=max_len)
        except Exception as exc:
            log.exception("Browser startup failed during get_page_snapshot")
            return json.dumps({"error": f"Failed to get page snapshot: {exc}"})
        return json.dumps({
            "snapshot": snapshot,
        })

    return run_in_pw_thread(_do)


def handle_click(args: dict, thread_id: str) -> str:
    async def _do():
        try:
            locator = await browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
                text=args.get("text"),
                thread_id=thread_id,
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        try:
            await locator.click(timeout=10_000)
            # Wait briefly for potential navigation or DOM changes
            page = await browser_mgr.ensure_page(thread_id)
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except PlaywrightTimeout:
            pass  # Page may not navigate — that's fine
        except Exception as exc:
            return json.dumps({
                "error": f"Click failed: {exc}",
                "snapshot": await browser_mgr.take_snapshot(thread_id=thread_id, max_text_length=2000),
            })

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(thread_id=thread_id),
        })

    return run_in_pw_thread(_do)


def handle_fill(args: dict, thread_id: str) -> str:
    value = args.get("value")
    if value is None:
        return json.dumps({"error": "value is required"})

    clear_first = args.get("clear_first", True)

    async def _do():
        try:
            locator = await browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
                thread_id=thread_id,
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

    return run_in_pw_thread(_do)


def handle_select_option(args: dict, thread_id: str) -> str:
    async def _do():
        try:
            locator = await browser_mgr.resolve_element(
                element_index=args.get("element_index"),
                selector=args.get("selector"),
                thread_id=thread_id,
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

    return run_in_pw_thread(_do)


def handle_press_key(args: dict, thread_id: str) -> str:
    key = args.get("key")
    if not key:
        return json.dumps({"error": "key is required"})

    async def _do():
        page = await browser_mgr.ensure_page(thread_id)

        # Optionally focus an element first
        el_index = args.get("element_index")
        selector = args.get("selector")
        if el_index is not None or selector is not None:
            try:
                locator = await browser_mgr.resolve_element(
                    element_index=el_index,
                    selector=selector,
                    thread_id=thread_id,
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
            "snapshot": await browser_mgr.take_snapshot(thread_id=thread_id),
        })

    return run_in_pw_thread(_do)


def handle_scroll(args: dict, thread_id: str) -> str:
    direction = args.get("direction", "down")
    amount = args.get("amount", "page")

    async def _do():
        page = await browser_mgr.ensure_page(thread_id)

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
            "snapshot": await browser_mgr.take_snapshot(thread_id=thread_id),
        })

    return run_in_pw_thread(_do)


def handle_go_back(args: dict, thread_id: str) -> str:
    async def _do():
        page = await browser_mgr.ensure_page(thread_id)
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15_000)
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            return json.dumps({"error": f"Go back failed: {exc}"})

        return json.dumps({
            "status": "ok",
            "snapshot": await browser_mgr.take_snapshot(thread_id=thread_id),
        })

    return run_in_pw_thread(_do)


def handle_wait(args: dict, thread_id: str) -> str:
    selector = args.get("selector")
    if not selector:
        return json.dumps({"error": "selector is required"})

    timeout_sec = args.get("timeout_seconds", 10)

    async def _do():
        page = await browser_mgr.ensure_page(thread_id)

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

    return run_in_pw_thread(_do)


def handle_execute_javascript(args: dict, thread_id: str) -> str:
    script = args.get("script")
    if not script:
        return json.dumps({"error": "script is required"})

    async def _do():
        page = await browser_mgr.ensure_page(thread_id)
        try:
            result = await page.evaluate(script)
            return json.dumps({
                "status": "ok",
                "result": result,
            })
        except Exception as exc:
            return json.dumps({"error": f"JavaScript execution failed: {exc}"})

    return run_in_pw_thread(_do)


def handle_search(args: dict, thread_id: str) -> str:
    query = args.get("query")
    if not query:
        return json.dumps({"error": "query is required"})

    num_results = args.get("num_results", 10)
    search_engine = args.get("search_engine", "google")

    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()

    if api_key:
        return _search_via_serpapi(query, num_results, search_engine, api_key)
    else:
        log.info("No SERPAPI_API_KEY set, falling back to browser-based search.")
        return _search_via_browser(query, num_results, thread_id)


def _search_via_serpapi(query: str, num_results: int, engine: str, api_key: str) -> str:
    """Perform a search using SerpAPI and return structured results."""
    params: dict[str, str | int] = {
        "q": query,
        "api_key": api_key,
        "num": min(num_results, 20),
    }

    if engine == "duckduckgo":
        params["engine"] = "duckduckgo"
    else:
        params["engine"] = "google"
        params["gl"] = "de"
        params["hl"] = "de"

    url = f"https://serpapi.com/search?{urlencode(params)}"
    log.info("SerpAPI search: engine=%s query=%r", engine, query)

    try:
        req = Request(url)
        req.add_header("Accept", "application/json")
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        log.warning("SerpAPI HTTP error %d: %s", exc.code, body)
        return json.dumps({
            "error": f"SerpAPI returned HTTP {exc.code}",
            "detail": body,
        })
    except (URLError, TimeoutError) as exc:
        log.warning("SerpAPI request failed: %s", exc)
        return json.dumps({"error": f"SerpAPI request failed: {exc}"})

    # Extract organic results
    results = []
    for item in data.get("organic_results", [])[:num_results]:
        result = {
            "position": item.get("position"),
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        # Include displayed link if available
        displayed_link = item.get("displayed_link")
        if displayed_link:
            result["displayed_link"] = displayed_link
        results.append(result)

    # Include knowledge graph if present
    knowledge_graph = data.get("knowledge_graph")
    kg_summary = None
    if knowledge_graph:
        kg_summary = {
            "title": knowledge_graph.get("title", ""),
            "type": knowledge_graph.get("type", ""),
            "description": knowledge_graph.get("description", ""),
        }
        # Include key attributes (address, phone, etc.)
        for key in ("address", "phone", "website", "rating", "hours",
                     "price", "review_count"):
            val = knowledge_graph.get(key)
            if val:
                kg_summary[key] = val

    # Include answer box if present
    answer_box = data.get("answer_box")
    answer = None
    if answer_box:
        answer = {
            "type": answer_box.get("type", ""),
            "title": answer_box.get("title", ""),
            "answer": answer_box.get("answer", answer_box.get("snippet", "")),
        }

    response: dict[str, Any] = {
        "status": "ok",
        "source": "serpapi",
        "engine": data.get("search_metadata", {}).get("google_url", ""),
        "query": query,
        "results": results,
        "total_results": len(results),
    }
    if kg_summary:
        response["knowledge_graph"] = kg_summary
    if answer:
        response["answer_box"] = answer

    return json.dumps(response)


def _search_via_browser(query: str, num_results: int, thread_id: str) -> str:
    """Perform a search by navigating the browser to Google.

    This is a fallback when no SerpAPI key is configured.  It may trigger
    bot-detection / CAPTCHAs on Google or DuckDuckGo.
    """
    search_url = f"https://www.google.com/search?q={quote_plus(query)}&num={num_results}"

    async def _do():
        try:
            page = await browser_mgr.ensure_page(thread_id)
        except Exception as exc:
            return json.dumps({"error": f"Failed to start browser: {exc}"})

        try:
            await page.goto(search_url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            return json.dumps({"error": f"Search navigation failed: {exc}"})

        # Check for bot detection
        try:
            body = await page.inner_text("body") or ""
        except Exception:
            body = ""

        bot_indicators = [
            "unusual traffic", "automated queries",
            "ungewöhnlichen datenverkehr", "/sorry/",
        ]
        is_blocked = any(ind in body.lower() for ind in bot_indicators)
        if "/sorry/" in page.url:
            is_blocked = True

        if is_blocked:
            return json.dumps({
                "status": "blocked",
                "source": "browser",
                "query": query,
                "message": (
                    "Google blocked this search (bot detection). "
                    "Configure a SERPAPI_API_KEY credential for reliable search, "
                    "or try navigating to a specific website directly."
                ),
                "snapshot": await browser_mgr.take_snapshot(
                    thread_id=thread_id, max_text_length=1000
                ),
            })

        # Extract results from the page via JavaScript
        results_js = """
        () => {
            const results = [];
            // Google organic results
            const items = document.querySelectorAll('div.g, div[data-hveid] div.tF2Cxc');
            for (const item of items) {
                const linkEl = item.querySelector('a[href]');
                const titleEl = item.querySelector('h3');
                const snippetEl = item.querySelector('[data-sncf], .VwiC3b, .IsZvec');
                if (linkEl && titleEl) {
                    results.push({
                        title: titleEl.innerText || '',
                        url: linkEl.href || '',
                        snippet: snippetEl ? snippetEl.innerText || '' : '',
                    });
                }
            }
            return results;
        }
        """
        try:
            extracted = await page.evaluate(results_js)
        except Exception:
            extracted = []

        if extracted:
            for i, r in enumerate(extracted[:num_results], 1):
                r["position"] = i
            return json.dumps({
                "status": "ok",
                "source": "browser",
                "query": query,
                "results": extracted[:num_results],
                "total_results": len(extracted[:num_results]),
            })

        # Fallback: return the page snapshot
        return json.dumps({
            "status": "ok",
            "source": "browser",
            "query": query,
            "results": [],
            "message": "Could not extract structured results. See snapshot.",
            "snapshot": await browser_mgr.take_snapshot(
                thread_id=thread_id, max_text_length=3000
            ),
        })

    return run_in_pw_thread(_do)


def handle_save_state(args: dict, thread_id: str) -> str:
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


def handle_load_state(args: dict, thread_id: str) -> str:
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
    "search": handle_search,
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
    "search": "query",
}


# ---------------------------------------------------------------------------
# gRPC servicer
# ---------------------------------------------------------------------------

class CapabilityServicer(capability_pb2_grpc.CapabilityServicer):

    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True, message="ok")

    def Invoke(self, request, context):
        tool = request.tool_name
        thread_id = request.thread_id or ""
        log.info("Invoke: %s (thread=%s)", tool, thread_id[:12] if thread_id else "default")

        handler = TOOL_HANDLERS.get(tool)
        if not handler:
            return capability_pb2.InvokeResponse(
                error=f"Unknown tool: {tool}. Available: {', '.join(TOOL_HANDLERS.keys())}"
            )

        # Parse credentials from config_json (injected by the orchestrator).
        # These are made available as environment variables for the duration
        # of this handler call so that handlers can read them via os.environ.
        config: dict[str, str] = {}
        if request.config_json:
            try:
                config = json.loads(request.config_json)
                if not isinstance(config, dict):
                    config = {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.warning("config_json was not valid JSON, ignoring.")

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
            # Inject credentials from config_json as env vars for this call.
            # Clean them up afterwards to avoid leaking across requests.
            injected_keys: list[str] = []
            for key, value in config.items():
                if isinstance(value, str) and key not in os.environ:
                    os.environ[key] = value
                    injected_keys.append(key)
            try:
                result = handler(args, thread_id)
            finally:
                for key in injected_keys:
                    os.environ.pop(key, None)
        except TimeoutError as exc:
            log.error("Tool %s timed out: %s", tool, exc)
            return capability_pb2.InvokeResponse(
                error=f"Tool '{tool}' timed out: {exc}"
            )
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
    try:
        run_in_pw_thread(browser_mgr.close, timeout=10)
    except TimeoutError:
        log.warning("Browser close timed out during shutdown.")
    _pw_loop.call_soon_threadsafe(_pw_loop.stop)
    _pw_thread.join(timeout=5)
    log.info("Server stopped.")


if __name__ == "__main__":
    serve()
