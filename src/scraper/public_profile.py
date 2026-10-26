"""Internal, CLI-only anonymous rendering policy, version public/v1.

The browser is offline behind a held, non-listening proxy socket. Only the
stateless broker connects to validated public addresses. This is not an OS
network sandbox or protection against a browser exploit.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
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

from scraper.content import clean_markdown_content, looks_like_bot_block
from scraper.safety import UrlRejected, ValidatedTarget, validate_url

PROFILE_ID = "public/v1"
DEBUG = False
CSP = "sandbox allow-scripts; worker-src 'none'; frame-src 'none'; child-src 'none'; object-src 'none'"
INIT = r"""(() => {
  Object.defineProperty(Document.prototype, 'cookie', {
    get() { return ''; }, set(value) {}, configurable: false, enumerable: true
  });
  for (const key of ['cookieStore', 'RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
    Object.defineProperty(globalThis, key, {value: undefined, writable: false, configurable: false});
  }
})();"""
CAPABILITY = """() => {
  const cookie = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
  if (!cookie || cookie.configurable || document.cookie !== '') return false;
  for (const key of ['cookieStore','RTCPeerConnection','webkitRTCPeerConnection','WebTransport']) {
    const d = Object.getOwnPropertyDescriptor(globalThis, key);
    if (!d || d.configurable || d.writable || typeof globalThis[key] !== 'undefined') return false;
  }
  try { localStorage.getItem('public-capability'); return false; }
  catch (e) { return e.name === 'SecurityError'; }
}"""


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


LIMITS = PublicLimits()


class Failure(StrEnum):
    INVALID_INPUT = "invalid_input"
    URL_REJECTED = "url_rejected"
    CREDENTIALS_IN_URL = "credentials_in_url"
    REDIRECT_REJECTED = "redirect_rejected"
    HTTP_STATUS = "http_status"
    UNSUPPORTED_CONTENT = "unsupported_content"
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


def serialize(result: PublicResult) -> str:
    text = json.dumps(asdict(result), ensure_ascii=True)
    if len(text.encode("utf-8")) + 1 > LIMITS.json_bytes:
        return json.dumps(asdict(failure_result(Failure.TOO_LARGE, result.elapsed_ms)))
    return text


def exit_code(result: PublicResult) -> int:
    if result.ok:
        return 0
    if result.failure in (Failure.INVALID_INPUT, Failure.URL_REJECTED, Failure.CREDENTIALS_IN_URL):
        return 2
    if result.failure == Failure.TIMEOUT:
        return 3
    if result.failure in (
        Failure.INTERNAL,
        Failure.ENFORCEMENT_UNAVAILABLE,
        Failure.CLEANUP_FAILED,
    ):
        return 4
    return 1


class Records:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.id = uuid.uuid4().hex
        self.stage = "input"
        self.started = time.monotonic()

    def counters(self, requests: int, body_bytes: int) -> None:
        if DEBUG:
            print(
                json.dumps(
                    {
                        "profile": PROFILE_ID,
                        "id": self.id,
                        "stage": "broker",
                        "event": "counters",
                        "requests": requests,
                        "body_bytes": body_bytes,
                        "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                    }
                ),
                file=self.stream,
                flush=True,
            )

    def emit(self, stage: str, event: str, outcome: str = "pending") -> None:
        self.stage = stage
        print(
            json.dumps(
                {
                    "profile": PROFILE_ID,
                    "id": self.id,
                    "stage": stage,
                    "event": event,
                    "outcome": outcome,
                    "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                }
            ),
            file=self.stream,
            flush=True,
        )


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
        self.resolvers = asyncio.Semaphore(8)

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

    async def fetch(self, url: str, method: str = "GET") -> Document:
        self.requests += 1
        if self.requests > LIMITS.requests:
            raise PublicError(Failure.TOO_LARGE)
        if method not in ("GET", "HEAD"):
            raise PublicError(Failure.URL_REJECTED)
        try:
            parts = urlsplit(url)
            if parts.username is not None or parts.password is not None:
                raise PublicError(Failure.CREDENTIALS_IN_URL)
            target = await self.validate(url)
        except (UrlRejected, ValueError, TimeoutError) as exc:
            raise PublicError(Failure.URL_REJECTED) from exc
        original = httpx.URL(target.url)
        pinned = original.copy_with(host=target.addresses[0])
        hostname = original.raw_host.decode("ascii")
        host = f"[{hostname}]" if ":" in hostname else hostname
        if target.port is not None:
            host += f":{target.port}"
        headers = {"host": host, "accept": "*/*", "user-agent": "scraper-public/v1"}
        extensions = {"sni_hostname": hostname} if target.scheme == "https" else {}
        try:
            async with (
                httpx.AsyncClient(
                    transport=_open_transport(),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=LIMITS.page_ms / 1000,
                ) as client,
                client.stream(method, pinned, headers=headers, extensions=extensions) as response,
            ):
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > LIMITS.response_bytes:
                    raise PublicError(Failure.TOO_LARGE)
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    body.extend(chunk)
                    self.bytes += len(chunk)
                    if len(body) > LIMITS.response_bytes or self.bytes > LIMITS.crawl_bytes:
                        raise PublicError(Failure.TOO_LARGE)
                return Document(
                    target.url, response.status_code, dict(response.headers), bytes(body)
                )
        except httpx.HTTPError as exc:
            raise PublicError(Failure.RENDER_FAILED) from exc

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
    def __init__(self, broker: Broker, document: Document):
        self.broker = broker
        self.document = document
        self.installed = False
        self.served = False
        self.checked = False
        self.failure: Failure | None = None

    async def route(self, route: Any) -> None:
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
                document = await self.broker.fetch(request.url, request.method)
            # Redirects are followed only by document prefetch, never by Chromium.
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
                    "access-control-allow-origin",
                )
            }
            if request.resource_type == "document":
                headers["content-security-policy"] = CSP
            await route.fulfill(status=document.status, headers=headers, body=document.body)
        except PublicError as exc:
            self.failure = self.failure or exc.failure
            await route.abort("blockedbyclient")
        except Exception:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            await route.abort("blockedbyclient")

    async def install(self, page: Any, context: Any, **_kwargs: Any) -> Any:
        from playwright.async_api import BrowserContext

        try:
            if not isinstance(context, BrowserContext):
                raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
            await context.set_offline(True)
            await context.clear_cookies()
            await context.add_init_script(script=INIT)
            await context.route_web_socket("**/*", self.websocket)
            await context.route("**/*", self.route)
            self.installed = True
        except Exception as exc:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            raise PublicError(self.failure) from exc
        return page

    async def websocket(self, ws: Any) -> None:
        await ws.close(code=1008, reason="public profile")

    async def inspect(self, page: Any, **_kwargs: Any) -> Any:
        try:
            if not await page.evaluate(CAPABILITY) or await page.context.cookies():
                raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
            self.checked = True
        except Exception as exc:
            self.failure = Failure.ENFORCEMENT_UNAVAILABLE
            raise PublicError(self.failure) from exc
        return page


def _check_runtime() -> None:
    try:
        pair = (importlib.metadata.version("crawl4ai"), importlib.metadata.version("playwright"))
    except importlib.metadata.PackageNotFoundError as exc:
        raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE) from exc
    if pair != ("0.9.0", "1.60.0"):
        raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)


async def _render(
    document: Document, broker: Broker, records: Records, deadline: float
) -> tuple[str, str]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    enforcement = Enforcement(broker, document)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as inert:
        inert.bind(("127.0.0.1", 0))
        proxy = f"http://127.0.0.1:{inert.getsockname()[1]}"
        config = BrowserConfig(
            headless=True,
            verbose=False,
            browser_mode="dedicated",
            use_managed_browser=False,
            use_persistent_context=False,
            storage_state=None,
            cookies=[],
            java_script_enabled=True,
            light_mode=True,
            ignore_https_errors=False,
            text_mode=False,
            accept_downloads=False,
            proxy_config={"server": proxy},
        )
        # Crawl4AI's decorator exposes a bound/unbound union to type checkers.
        crawler: Any = AsyncWebCrawler(config=config)
        try:
            async with asyncio.timeout_at(deadline):
                strategy = getattr(crawler, "crawler_strategy", None)
                if strategy is None or not callable(getattr(strategy, "set_hook", None)):
                    raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
                strategy.set_hook("on_page_context_created", enforcement.install)
                strategy.set_hook("before_retrieve_html", enforcement.inspect)
                records.emit("browser", "start")
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
                        delay_before_return_html=1.5,
                        word_count_threshold=1,
                        verbose=False,
                    ),
                )
                if enforcement.failure:
                    raise PublicError(enforcement.failure)
                if not (enforcement.installed and enforcement.served and enforcement.checked):
                    raise PublicError(Failure.ENFORCEMENT_UNAVAILABLE)
                if not result.success:
                    raise PublicError(Failure.RENDER_FAILED)
                html = result.html or ""
                if len(html.encode("utf-8")) > LIMITS.html_bytes:
                    raise PublicError(Failure.TOO_LARGE)
                markdown = result.markdown.raw_markdown if result.markdown else ""
                markdown = markdown[: LIMITS.markdown_chars]
                if looks_like_bot_block(markdown):
                    raise PublicError(Failure.BOT_DETECTED)
                if not markdown.strip():
                    raise PublicError(Failure.RENDER_FAILED)
                records.emit("browser", "end", "ok")
                return html, clean_markdown_content(markdown)[: LIMITS.markdown_chars]
        finally:
            records.emit("cleanup", "start")
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
                records.emit("cleanup", "end", Failure.CLEANUP_FAILED)
                raise PublicError(Failure.CLEANUP_FAILED) from close_error
            records.emit("cleanup", "end", "ok")


async def run_public_scrape(url: str, timeout: float, records: Records) -> PublicResult:
    started = time.monotonic()
    try:
        if timeout <= 0 or not isinstance(url, str) or not url:
            raise PublicError(Failure.INVALID_INPUT)
        deadline = asyncio.get_running_loop().time() + timeout
        _check_runtime()
        with isolate_process(), _vendor_output():
            broker = Broker()
            try:
                async with asyncio.timeout_at(deadline):
                    records.emit("fetch", "start")
                    document = await broker.document(url)
                    records.emit("fetch", "end", "ok")
                html, markdown = await _render(document, broker, records, deadline)
            finally:
                records.counters(broker.requests, broker.bytes)
        result = PublicResult(
            ok=True,
            url=url,
            final_url=document.url,
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
    records.emit("public", "start")
    try:
        if args.stdin:
            try:
                data = json.loads(sys.stdin.read(1_048_577))
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
    text = serialize(result)
    # Serialization itself may exceed the cap even when the HTML does not.
    if json.loads(text)["failure"] == Failure.TOO_LARGE:
        result = failure_result(Failure.TOO_LARGE, result.elapsed_ms)
    records.emit("public", "end", result.failure or "ok")
    print(text)
    raise SystemExit(exit_code(result))
