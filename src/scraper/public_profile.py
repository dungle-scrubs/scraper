"""Internal, CLI-only anonymous rendering policy, version public/v1.

Firefox uses an owned native startup policy and a held, non-listening HTTP
proxy. Routed requests are fulfilled by the stateless validated-address broker.
This is not an OS network sandbox or protection against a browser exploit.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import ipaddress
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urljoin, urlsplit

import httpx

from scraper.cli import EXIT_INTERNAL, EXIT_INVALID_INPUT, EXIT_OK, EXIT_SCRAPE_FAILED, EXIT_TIMEOUT
from scraper.content import clean_markdown_content, looks_like_bot_block
from scraper.safety import UrlRejected, ValidatedTarget, _format_netloc, validate_url

PROFILE_ID = "public/v1"
DEBUG = False
CSP = "sandbox allow-scripts; worker-src 'none'; frame-src 'none'; child-src 'none'; object-src 'none'"
# Playwright 1.60's native Firefox policy provider reads this file before UI startup.
NATIVE_POLICY = {
    "policies": {
        "Cookies": {"Behavior": "reject", "BehaviorPrivateBrowsing": "reject", "Locked": True},
        "Preferences": {
            name: {"Value": value, "Status": "locked", "Type": kind}
            for name, value, kind in (
                ("media.peerconnection.enabled", False, "boolean"),
                ("network.webtransport.enabled", False, "boolean"),
                ("network.http.http3.enable", False, "boolean"),
                ("network.dns.disablePrefetch", True, "boolean"),
                ("network.http.speculative-parallel-limit", 0, "number"),
                ("network.predictor.enabled", False, "boolean"),
                ("network.prefetch-next", False, "boolean"),
                ("network.proxy.allow_hijacking_localhost", True, "boolean"),
            )
        },
    }
}
READINESS_URL = "https://public-profile.invalid/readiness"
READINESS_WS = "wss://public-profile.invalid/readiness"


@dataclass(frozen=True)
class PublicLimits:
    redirects: int = 5
    validation_seconds: float = 5
    page_ms: int = 15_000
    markdown_chars: int = 1_000_000
    response_bytes: int = 5_000_000
    crawl_bytes: int = 20_000_000
    requests: int = 200
    html_bytes: int = 2 * 1024 * 1024
    json_bytes: int = 8 * 1024 * 1024
    cleanup_seconds: float = 10
    resolver_workers: int = 8
    render_delay_seconds: float = 1.5
    stdin_chars: int = 1_048_576


LIMITS = PublicLimits()


class Failure(StrEnum):
    INVALID_INPUT = "invalid_input"
    URL_REJECTED = "url_rejected"
    CREDENTIALS_IN_URL = "credentials_in_url"
    REDIRECT_REJECTED = "redirect_rejected"
    NAVIGATION_REJECTED = "navigation_rejected"
    HTTP_STATUS = "http_status"
    UNSUPPORTED_CONTENT = "unsupported_content"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    TOO_LARGE = "too_large"
    BOT_DETECTED = "bot_detected"
    RENDER_FAILED = "render_failed"
    ENFORCEMENT_UNAVAILABLE = "enforcement_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"
    INTERNAL = "internal"


class PublicError(Exception):
    def __init__(self, failure: Failure):
        self.failure = failure
        super().__init__(failure.value)


@dataclass(frozen=True)
class PublicResult:
    ok: bool = False
    profile: str = PROFILE_ID
    failure: Failure | None = None
    error: str | None = None
    url: str = ""
    final_url: str = ""
    html: str = ""
    markdown: str = ""
    source: str = "none"
    bot_detected: bool = False
    elapsed_ms: float = 0
    attempts: list[str] = field(default_factory=list)


def failure_result(failure: Failure, elapsed_ms: float = 0) -> PublicResult:
    # Rejected inputs and upstream exception text never enter the error record.
    return PublicResult(
        failure=failure,
        error=failure.value,
        elapsed_ms=elapsed_ms,
        bot_detected=failure == Failure.BOT_DETECTED,
    )


def serialize(result: PublicResult) -> tuple[PublicResult, str]:
    text = json.dumps(asdict(result), ensure_ascii=True)
    if len(text) + 1 > LIMITS.json_bytes:
        result = failure_result(Failure.TOO_LARGE, result.elapsed_ms)
        text = json.dumps(asdict(result), ensure_ascii=True)
    return result, text


def exit_code(result: PublicResult) -> int:
    if result.ok:
        return EXIT_OK
    if result.failure in (Failure.INVALID_INPUT, Failure.URL_REJECTED, Failure.CREDENTIALS_IN_URL):
        return EXIT_INVALID_INPUT
    if result.failure == Failure.TIMEOUT:
        return EXIT_TIMEOUT
    if result.failure in (
        Failure.INTERNAL,
        Failure.ENFORCEMENT_UNAVAILABLE,
        Failure.CLEANUP_FAILED,
    ):
        return EXIT_INTERNAL
    return EXIT_SCRAPE_FAILED


class Stage(StrEnum):
    PUBLIC = "public"
    FETCH = "fetch"
    BROWSER = "browser"
    ENFORCEMENT = "enforcement"
    CLEANUP = "cleanup"
    BROKER = "broker"


class Event(StrEnum):
    START = "start"
    END = "end"
    COUNTERS = "counters"


class Records:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.id = uuid.uuid4().hex
        self.stage = Stage.PUBLIC
        self.started = time.monotonic()

    def write(
        self,
        stage: Stage,
        event: Event,
        outcome: str | None = None,
        requests: int | None = None,
        body_bytes: int | None = None,
    ) -> None:
        self.stage = stage
        payload: dict[str, Any] = {
            "profile": PROFILE_ID,
            "id": self.id,
            "stage": stage,
            "event": event,
            "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
        }
        if outcome is not None:
            payload["outcome"] = outcome
        if requests is not None:
            payload["requests"] = requests
            payload["body_bytes"] = body_bytes
        print(json.dumps(payload), file=self.stream, flush=True)

    def counters(self, requests: int, body_bytes: int) -> None:
        if DEBUG:
            self.write(Stage.BROKER, Event.COUNTERS, requests=requests, body_bytes=body_bytes)

    def emit(self, stage: Stage, event: Event, outcome: str = "pending") -> None:
        self.write(stage, event, outcome)


@contextlib.contextmanager
def _vendor_output():
    # Logging handlers can retain the original stderr across a stream redirect.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            yield
    finally:
        logging.disable(previous)


def _browser_locator() -> str:
    value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if value == "0":
        import playwright

        return str(Path(playwright.__file__).parent / "driver/package/.local-browsers")
    if value:
        return str((Path(os.environ.get("INIT_CWD") or Path.cwd()) / value).resolve())
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library/Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    return str((base / "ms-playwright").resolve())


@contextlib.contextmanager
def isolate_process():
    """Discard ambient configuration in this disposable CLI process."""
    locator = _browser_locator()
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="scraper-public-", dir="/tmp") as directory:
        root = Path(directory)
        for name in ("home", "tmp", "crawl", "cwd"):
            (root / name).mkdir()
        os.environ.clear()
        os.environ.update(
            {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(root / "home"),
                "TMPDIR": str(root / "tmp"),
                "CRAWL4_AI_BASE_DIRECTORY": str(root / "crawl"),
                "PYTHON_DOTENV_DISABLED": "1",
                "PLAYWRIGHT_BROWSERS_PATH": locator,
                "LANG": "en_US.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        tempfile.tempdir = str(root / "tmp")
        os.chdir(root / "cwd")
        try:
            yield root
        finally:
            os.chdir(original_cwd)
            tempfile.tempdir = None


def _open_transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport(verify=True)


@dataclass(frozen=True)
class Document:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class Broker:
    def __init__(self):
        self.requests = 0
        self.bytes = 0
        self.resolvers = asyncio.Semaphore(LIMITS.resolver_workers)

    async def validate(self, url: str) -> ValidatedTarget:
        # OS DNS is not cancellable. Unlike ThreadPoolExecutor workers, these
        # bounded CLI-owned daemon threads cannot hold interpreter shutdown.
        # A timed-out worker keeps its slot until it actually finishes.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ValidatedTarget] = loop.create_future()
        async with asyncio.timeout(LIMITS.validation_seconds):
            await self.resolvers.acquire()

            def deliver(target: ValidatedTarget | None, error: Exception | None) -> None:
                self.resolvers.release()
                if future.done():
                    return
                if error is not None:
                    future.set_exception(error)
                else:
                    assert target is not None
                    future.set_result(target)

            def resolve() -> None:
                target = None
                error = None
                try:
                    target = validate_url(url)
                except Exception as exc:
                    error = exc
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(deliver, target, error)

            threading.Thread(target=resolve, name="public-resolve", daemon=True).start()
            return await future

    def count_attempt(self) -> None:
        self.requests += 1
        if self.requests > LIMITS.requests:
            raise PublicError(Failure.TOO_LARGE)

    async def fetch(self, url: str, method: str = "GET") -> Document:
        self.count_attempt()
        if method not in ("GET", "HEAD"):
            raise PublicError(Failure.URL_REJECTED)
        try:
            parts = urlsplit(url)
            if parts.username is not None or parts.password is not None:
                raise PublicError(Failure.CREDENTIALS_IN_URL)
            target = await self.validate(url)
        except TimeoutError as exc:
            raise PublicError(Failure.TIMEOUT) from exc
        except (UrlRejected, ValueError) as exc:
            raise PublicError(Failure.URL_REJECTED) from exc
        # Prefer IPv6 deterministically, but keep IPv4-only hosts usable. Retry
        # only connection establishment, never a partially received response.
        addresses = sorted(
            set(target.addresses),
            key=lambda value: (
                ipaddress.ip_address(value).version != 6,
                int(ipaddress.ip_address(value)),
            ),
        )
        for index, address in enumerate(addresses):
            if index:
                self.count_attempt()
            try:
                return await self.fetch_address(target, address, method)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if index == len(addresses) - 1:
                    raise PublicError(Failure.RENDER_FAILED) from exc
            except httpx.HTTPError as exc:
                raise PublicError(Failure.RENDER_FAILED) from exc
        raise PublicError(Failure.RENDER_FAILED)

    async def fetch_address(self, target: ValidatedTarget, address: str, method: str) -> Document:
        original = httpx.URL(target.url)
        pinned = original.copy_with(host=address)
        hostname = original.raw_host.decode("ascii")
        headers = {
            "host": _format_netloc(hostname, target.port),
            "accept": "*/*",
            "accept-encoding": "identity",
            "user-agent": "scraper-public/v1",
        }
        extensions = {"sni_hostname": hostname} if target.scheme == "https" else {}
        async with (
            httpx.AsyncClient(
                transport=_open_transport(),
                trust_env=False,
                follow_redirects=False,
                timeout=LIMITS.page_ms / 1000,
            ) as client,
            client.stream(method, pinned, headers=headers, extensions=extensions) as response,
        ):
            if response.headers.get("content-encoding", "identity").strip().lower() not in (
                "",
                "identity",
            ):
                raise PublicError(Failure.UNSUPPORTED_ENCODING)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > LIMITS.response_bytes:
                raise PublicError(Failure.TOO_LARGE)
            body = bytearray()
            # Read raw bytes so no HTTPX decompressor can allocate before our cap.
            async for chunk in response.aiter_raw(chunk_size=65_536):
                body.extend(chunk)
                self.bytes += len(chunk)
                if len(body) > LIMITS.response_bytes or self.bytes > LIMITS.crawl_bytes:
                    raise PublicError(Failure.TOO_LARGE)
            return Document(target.url, response.status_code, dict(response.headers), bytes(body))

    async def document(self, url: str) -> Document:
        for hop in range(LIMITS.redirects + 1):
            try:
                document = await self.fetch(url)
            except PublicError as exc:
                if hop and exc.failure in (Failure.URL_REJECTED, Failure.CREDENTIALS_IN_URL):
                    raise PublicError(Failure.REDIRECT_REJECTED) from exc
                raise
            if 300 <= document.status < 400:
                location = document.headers.get("location")
                if not location or hop == LIMITS.redirects:
                    raise PublicError(Failure.REDIRECT_REJECTED)
                url = urljoin(document.url, location)
                continue
            if not 200 <= document.status < 300:
                raise PublicError(Failure.HTTP_STATUS)
            if document.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in (
                "text/html",
                "application/xhtml+xml",
            ):
                raise PublicError(Failure.UNSUPPORTED_CONTENT)
            return document
        raise PublicError(Failure.REDIRECT_REJECTED)


def _request_key(url: str) -> tuple[str, bytes, int | None, bytes]:
    parsed = httpx.URL(url)
    return parsed.scheme, parsed.raw_host, parsed.port, parsed.raw_path


class Enforcement:
    def __init__(self, broker: Broker, document: Document, policy_path: Path, records: Records):
        self.broker = broker
        self.document = document
        self.policy_path = policy_path
        self.records = records
        self.installed = False
        self.navigation_started = False
        self.served = False
        self.checked = False
        self.failure: Failure | None = None
        self.page: Any = None
        self.root: Any = None
        self.navigations = 0
        self.captured: str | None = None
        self.readiness_ws = False

    async def route(self, route: Any) -> None:
        if not self.navigation_started:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            await route.abort("blockedbyclient")
            return
        request = route.request
        if request.method not in ("GET", "HEAD"):
            await route.abort("blockedbyclient")
            return
        try:
            if (
                not self.served
                and _request_key(request.url) == _request_key(self.document.url)
                and request.is_navigation_request()
            ):
                document = self.document
                self.served = True
            else:
                if (
                    request.is_navigation_request()
                    and request.frame == request.frame.page.main_frame
                ):
                    raise PublicError(Failure.NAVIGATION_REJECTED)
                document = await self.broker.fetch(request.url, request.method)
            # Redirects are followed only by document prefetch, never by the browser.
            if 300 <= document.status < 400:
                await route.abort("blockedbyclient")
                return
            headers = {
                k: v
                for k, v in document.headers.items()
                if k
                in (
                    "content-type",
                    "content-language",
                )
            }
            headers["access-control-allow-origin"] = "null"
            headers["access-control-allow-credentials"] = "true"
            if request.resource_type == "document":
                headers["content-security-policy"] = CSP
            await route.fulfill(status=document.status, headers=headers, body=document.body)
        except PublicError as exc:
            self.failure = self.failure or exc.failure
            await route.abort("blockedbyclient")
        except Exception:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            await route.abort("blockedbyclient")

    async def native_readiness(self, context: Any) -> None:
        # Check the entire owned file, then test native effects in a fresh secure
        # document with no caller scripts. Pinned native policy consumers supply
        # the lock/transport argument; this is not a privileged preference getter.
        if json.loads(self.policy_path.read_text(encoding="utf-8")) != NATIVE_POLICY:
            raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
        await context.route_web_socket("**/*", self.websocket)
        trusted = await context.new_page()
        try:

            async def readiness_route(route: Any) -> None:
                if route.request.url == READINESS_URL and route.request.method == "GET":
                    await route.fulfill(
                        status=200,
                        headers={
                            "content-type": "text/html",
                            "set-cookie": "readiness_response=synthetic; Secure; SameSite=None",
                        },
                        body="<!doctype html><html><body>Public policy readiness</body></html>",
                    )
                else:
                    await route.abort("blockedbyclient")

            await trusted.route("**/*", readiness_route)
            await trusted.goto(READINESS_URL, wait_until="load")
            ready = await trusted.evaluate("""async () => {
                document.cookie='readiness_script=synthetic; Secure; SameSite=None';
                const closed = await new Promise(resolve => {
                    const ws = new WebSocket('wss://public-profile.invalid/readiness');
                    ws.onclose=()=>resolve(true);
                    ws.onerror=()=>{};
                    setTimeout(()=>resolve(false),1000);
                });
                return {
                    secure: isSecureContext,
                    rtc: typeof RTCPeerConnection,
                    rtcAlias: typeof webkitRTCPeerConnection,
                    transport: typeof WebTransport,
                    cookies: document.cookie,
                    closed
                };
            }""")
            if (
                ready
                != {
                    "secure": True,
                    "rtc": "undefined",
                    "rtcAlias": "undefined",
                    "transport": "undefined",
                    "cookies": "",
                    "closed": True,
                }
                or not self.readiness_ws
                or await context.cookies()
            ):
                raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
        finally:
            await trusted.close()

    async def install(self, page: Any, context: Any, **_kwargs: Any) -> Any:
        from playwright.async_api import BrowserContext

        self.records.emit(Stage.ENFORCEMENT, Event.START)
        try:
            if (
                not isinstance(context, BrowserContext)
                or context.browser.browser_type.name != "firefox"
                or context.browser.version != "150.0.2"
            ):
                raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
            await context.clear_cookies()
            await self.native_readiness(context)
            self.page = page
            page.on("framenavigated", self.navigated)
            await context.route("**/*", self.route)
            self.installed = True
        except asyncio.CancelledError:
            self.records.emit(Stage.ENFORCEMENT, Event.END, Failure.CANCELLED)
            raise
        except Exception as exc:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            self.records.emit(Stage.ENFORCEMENT, Event.END, self.failure)
            raise PublicError(self.failure) from exc
        self.records.emit(Stage.ENFORCEMENT, Event.END, "ok")
        return page

    async def before_goto(self, page: Any, **_kwargs: Any) -> Any:
        if not self.installed or page is not self.page:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            raise PublicError(self.failure)
        self.navigation_started = True
        return page

    async def websocket(self, ws: Any) -> None:
        if ws.url == READINESS_WS:
            self.readiness_ws = True
        await ws.close(code=1008, reason="public profile")

    def navigated(self, frame: Any) -> None:
        if frame == self.page.main_frame:
            self.navigations += 1
            if self.navigations != 1 or _request_key(frame.url) != _request_key(self.document.url):
                self.failure = Failure.NAVIGATION_REJECTED

    async def identify_document(self, page: Any, **_kwargs: Any) -> Any:
        # Native selector queries use Playwright's utility world. The retained
        # handle binds later attachment checks to this DOM root.
        self.root = await page.locator(":root").element_handle()
        if self.root is None:
            raise PublicError(Failure.NAVIGATION_REJECTED)
        await self.attest_current()
        return page

    async def attest_current(self) -> str:
        if self.failure:
            raise PublicError(self.failure)
        if not self.installed or not self.served or self.root is None:
            raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
        if self.navigations != 1 or _request_key(self.page.url) != _request_key(self.document.url):
            raise PublicError(Failure.NAVIGATION_REJECTED)
        # A native selector scoped to the retained handle must still find that
        # node directly under its Document. Detached/replaced roots do not match.
        if await self.root.query_selector("xpath=self::*[parent::node()[not(self::*)]]") is None:
            raise PublicError(Failure.NAVIGATION_REJECTED)
        return self.page.url

    async def inspect(self, page: Any, **_kwargs: Any) -> Any:
        try:
            await self.attest_current()
            if await page.context.cookies():
                raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
            self.checked = True
        except PublicError as exc:
            self.failure = self.failure or exc.failure
            raise PublicError(self.failure) from exc
        except Exception as exc:
            self.failure = self.failure or Failure.ENFORCEMENT_UNAVAILABLE
            raise PublicError(self.failure) from exc
        return page

    async def capture(self, page: Any, html: str, **_kwargs: Any) -> Any:
        if not self.checked:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            raise PublicError(self.failure)
        await self.inspect(page)
        self.captured = html
        return page


def _check_runtime() -> None:
    try:
        pair = (importlib.metadata.version("crawl4ai"), importlib.metadata.version("playwright"))
    except importlib.metadata.PackageNotFoundError as exc:
        raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE) from exc
    if pair != ("0.9.0", "1.60.0"):
        raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)


async def _render(
    document: Document, broker: Broker, records: Records, deadline: float, policy_path: Path
) -> tuple[str, str, str]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    enforcement = Enforcement(broker, document, policy_path, records)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as inert:
        inert.bind(("127.0.0.1", 0))
        proxy = f"http://127.0.0.1:{inert.getsockname()[1]}"
        config = BrowserConfig(
            browser_type="firefox",
            headless=True,
            verbose=False,
            browser_mode="dedicated",
            use_managed_browser=False,
            use_persistent_context=False,
            storage_state=None,
            cookies=[],
            java_script_enabled=True,
            light_mode=False,
            ignore_https_errors=False,
            text_mode=False,
            accept_downloads=False,
            proxy_config={"server": proxy},
        )
        # Crawl4AI's decorator exposes a bound/unbound union to type checkers.
        crawler: Any = AsyncWebCrawler(config=config)
        outcome: str = Failure.INTERNAL
        records.emit(Stage.BROWSER, Event.START)
        try:
            async with asyncio.timeout_at(deadline):
                strategy = getattr(crawler, "crawler_strategy", None)
                if strategy is None or not callable(getattr(strategy, "set_hook", None)):
                    raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
                strategy.set_hook("on_page_context_created", enforcement.install)
                strategy.set_hook("before_goto", enforcement.before_goto)
                strategy.set_hook("after_goto", enforcement.identify_document)
                strategy.set_hook("before_retrieve_html", enforcement.inspect)
                strategy.set_hook("before_return_html", enforcement.capture)
                try:
                    await crawler.start()
                except Exception as exc:
                    raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE) from exc
                result = await crawler.arun(
                    url=document.url,
                    config=CrawlerRunConfig(
                        cache_mode=CacheMode.DISABLED,
                        check_robots_txt=False,
                        process_iframes=False,
                        remove_overlay_elements=False,
                        page_timeout=LIMITS.page_ms,
                        wait_until="domcontentloaded",
                        delay_before_return_html=LIMITS.render_delay_seconds,
                        word_count_threshold=1,
                        verbose=False,
                    ),
                )
                if enforcement.failure:
                    raise PublicError(enforcement.failure)
                if not (
                    enforcement.installed
                    and enforcement.served
                    and enforcement.checked
                    and enforcement.captured is not None
                ):
                    raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
                await enforcement.attest_current()
                if not result.success:
                    raise PublicError(Failure.RENDER_FAILED)
                html = result.html or ""
                if html != enforcement.captured:
                    raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
                if len(html.encode("utf-8")) > LIMITS.html_bytes:
                    raise PublicError(Failure.TOO_LARGE)
                markdown = result.markdown.raw_markdown if result.markdown else ""
                markdown = markdown[: LIMITS.markdown_chars]
                if looks_like_bot_block(markdown):
                    raise PublicError(Failure.BOT_DETECTED)
                if not markdown.strip():
                    raise PublicError(Failure.RENDER_FAILED)
                captured_url = result.redirected_url or document.url
                if _request_key(captured_url) != _request_key(document.url):
                    raise PublicError(Failure.NAVIGATION_REJECTED)
                outcome = "ok"
                return html, clean_markdown_content(markdown)[: LIMITS.markdown_chars], captured_url
        except PublicError as exc:
            outcome = exc.failure
            raise
        except TimeoutError:
            outcome = Failure.TIMEOUT
            raise
        except asyncio.CancelledError:
            outcome = Failure.CANCELLED
            raise
        finally:
            records.emit(Stage.BROWSER, Event.END, outcome)
            records.emit(Stage.CLEANUP, Event.START)
            deadline = time.monotonic() + LIMITS.cleanup_seconds
            close_error: BaseException | None = None
            try:
                # Reserve part of the same grace for stopping the owned driver
                # if a browser/context close fails before Crawl4AI reaches it.
                await asyncio.wait_for(crawler.close(), LIMITS.cleanup_seconds - 2)
            except BaseException as exc:
                close_error = exc
            manager = getattr(getattr(crawler, "crawler_strategy", None), "browser_manager", None)
            driver = getattr(manager, "playwright", None)
            if driver is not None:
                try:
                    await asyncio.wait_for(driver.stop(), max(0, deadline - time.monotonic()))
                except BaseException as exc:
                    close_error = close_error or exc
            if close_error is not None:
                records.emit(Stage.CLEANUP, Event.END, Failure.CLEANUP_FAILED)
                raise PublicError(Failure.CLEANUP_FAILED) from close_error
            records.emit(Stage.CLEANUP, Event.END, "ok")


async def run_public_scrape(url: str, timeout: float, records: Records) -> PublicResult:
    started = time.monotonic()
    try:
        if timeout <= 0 or not isinstance(url, str) or not url:
            raise PublicError(Failure.INVALID_INPUT)
        deadline = asyncio.get_running_loop().time() + timeout
        _check_runtime()
        with isolate_process() as root, _vendor_output():
            policy_path = root / "firefox-policies.json"
            policy_path.write_text(json.dumps(NATIVE_POLICY), encoding="utf-8")
            os.environ["PLAYWRIGHT_FIREFOX_POLICIES_JSON"] = str(policy_path)
            broker = Broker()
            try:
                async with asyncio.timeout_at(deadline):
                    records.emit(Stage.FETCH, Event.START)
                    try:
                        document = await broker.document(url)
                    except PublicError as exc:
                        records.emit(Stage.FETCH, Event.END, exc.failure)
                        raise
                    except asyncio.CancelledError:
                        records.emit(Stage.FETCH, Event.END, Failure.CANCELLED)
                        raise
                    except Exception:
                        records.emit(Stage.FETCH, Event.END, Failure.INTERNAL)
                        raise
                    records.emit(Stage.FETCH, Event.END, "ok")
                html, markdown, final_url = await _render(
                    document, broker, records, deadline, policy_path
                )
            finally:
                records.counters(broker.requests, broker.bytes)
        result = PublicResult(
            ok=True,
            url=url,
            final_url=final_url,
            html=html,
            markdown=markdown,
            source="crawl4ai",
            elapsed_ms=(time.monotonic() - started) * 1000,
            attempts=["crawl4ai public"],
        )
    except asyncio.CancelledError:
        result = failure_result(Failure.CANCELLED, (time.monotonic() - started) * 1000)
    except TimeoutError:
        result = failure_result(Failure.TIMEOUT, (time.monotonic() - started) * 1000)
    except PublicError as exc:
        result = failure_result(exc.failure, (time.monotonic() - started) * 1000)
    except Exception:
        result = failure_result(Failure.INTERNAL, (time.monotonic() - started) * 1000)
    return result


def cmd_public(args: Any) -> None:
    # The CLI process is disposable; deferred vendor/asyncio logs must stay quiet
    # through event-loop shutdown as well as during the crawl itself.
    logging.disable(logging.CRITICAL)
    records = Records(sys.stderr)
    records.emit(Stage.PUBLIC, Event.START)
    try:
        if args.stdin:
            try:
                raw = sys.stdin.read(LIMITS.stdin_chars + 1)
                if len(raw) > LIMITS.stdin_chars:
                    raise PublicError(Failure.INVALID_INPUT)
                data = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise PublicError(Failure.INVALID_INPUT) from exc
            if not isinstance(data, dict):
                raise PublicError(Failure.INVALID_INPUT)
            url = data.get("url")
        else:
            url = args.url

        async def run() -> PublicResult:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            assert task is not None
            cancelling = False

            def cancel() -> None:
                nonlocal cancelling
                if not cancelling:
                    cancelling = True
                    task.cancel()

            try:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(signum, cancel)
            except (NotImplementedError, RuntimeError) as exc:
                raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE) from exc
            try:
                return await run_public_scrape(url, args.timeout, records)
            finally:
                for signum in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(signum)

        result = asyncio.run(run())
    except PublicError as exc:
        result = failure_result(exc.failure)
    result, text = serialize(result)
    records.emit(Stage.PUBLIC, Event.END, result.failure or "ok")
    print(text)
    raise SystemExit(exit_code(result))
