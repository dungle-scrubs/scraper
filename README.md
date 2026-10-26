# scraper

Get cleaned Markdown for the tools that collect your pages.

Use the CLI, Python library or HTTP service to render pages with Crawl4AI,
remove scraper artifacts and detect bot challenges. The CLI's `public` profile
also returns rendered HTML and enforces an anonymous, public-address policy.
For example, it rejects loopback targets before launching Chromium:

```console
$ scraper scrape --profile public http://127.0.0.1/
{"ok": false, "profile": "public/v1", "failure": "url_rejected", "error": "url_rejected", ...}
```

## Install

Python 3.12 or newer is required. For `public/v1`, run these commands from this
source checkout, which contains `pyproject.toml` and `uv.lock`:

```bash
uv sync --frozen
uv run --frozen playwright install chromium
uv run --frozen scraper scrape --help
```

The checkout lock selects Crawl4AI 0.9.0 and Playwright 1.60.0. That Playwright
installation supplies Chromium 148.0.7778.96. The public runtime guard rejects
other versions with `enforcement_unavailable`. On Linux, use
`uv run --frozen playwright install --with-deps chromium` for system dependencies.
Scraping never downloads a browser. Use `uv run --frozen` before the CLI examples
below to run this checkout's executable.

The package is `dungle-scrubs-scraper`. The existing PyPI 0.2.1 release is not an
installation path for this new flag. Public mode requires this source checkout
until a separately authorized release. Default CLI and library behavior is
preserved, but this checkout raises dependency minimums, including Crawl4AI
0.9.0 and Playwright 1.60.0.

## Usage

### Collect a page as Markdown

```bash
scraper scrape https://example.com
```

The default pipeline tries Crawl4AI, detects bot challenges, then cleans the
Markdown. Jina fallback runs only when `SCRAPER_JINA_ENABLED=true`.

### Parse a public page after JavaScript runs

```bash
scraper scrape --profile public --timeout 30 https://example.com
```

The JSON includes `profile: "public/v1"`, `html`, `markdown` and `final_url`.
`html` contains the DOM captured after JavaScript runs, including rendered
links and article structure. Pages that need normal origin privileges, workers,
frames or authentication can fail or render incompletely under this profile.

### Send the URL on stdin

```bash
echo '{"url": "https://example.com"}' | scraper scrape --profile public --stdin
```

`--stdin` also works with the default profile. Logs go to stderr; stdout holds
one JSON object.

### Call the library

```python
from scraper.scraper import scrape

result = await scrape("https://example.com")
print(result.markdown)
```

The library uses the configured pipeline. The public profile is CLI-only.

### Call the HTTP service

```bash
scraper serve
```

The service defaults to `127.0.0.1:8020`. It exposes `GET /health` and
`POST /scrape` with a JSON body such as `{"url": "https://example.com"}`.
The service uses the configured pipeline; it has no public-profile option.

```python
import httpx

response = httpx.post("http://127.0.0.1:8020/scrape", json={"url": "https://example.com"})
markdown = response.json()["markdown"]
```

## Reference

### Commands and results

```text
scraper scrape [--profile {default,public}] [--stdin] [--timeout SECONDS] [URL]
scraper serve
```

The default CLI result has `ok`, `markdown`, `source`, `url`, `bot_detected`,
`error`, `elapsed_ms` and `attempts`. The library and service retain their
existing result fields without the CLI's `ok` field. `source` is `crawl4ai`,
`jina` or `none`.

The public result adds `profile`, `failure`, `final_url` and `html`.
On success, `failure` and `error` are null and `source` is `crawl4ai`.
On failure, content and URL fields are empty, `source` is `none`, and `failure`
and `error` contain the same stable code. There is one attempt and no fallback.
`bot_detected` is true for `bot_detected` failures.

| Exit | Default profile | Public profile |
|---|---|---|
| 0 | Content scraped | Content scraped |
| 1 | Backends exhausted | `redirect_rejected`, `navigation_rejected`, `http_status`, `unsupported_content`, `unsupported_encoding`, `too_large`, `bot_detected`, `render_failed`, `cancelled` |
| 2 | Invalid input | `invalid_input`, `url_rejected`, `credentials_in_url` |
| 3 | Global timeout | `timeout`, including per-target DNS validation and the global deadline |
| 4 | Internal error | `internal`, `enforcement_unavailable`, `cleanup_failed` |

The table lists every public failure code. Errors never include upstream
exception text or a rejected URL. A failed subresource fetch fails the whole
page with empty content fields. Public mode does not return best-effort success
with partial content after a subresource failure.

### Public profile isolation

`public/v1` ignores `.env`, inherited scraper settings, proxy credentials,
remote-browser configuration and user browser state. It does not import the
configured pipeline or its settings. Each invocation has a temporary home,
working directory, browser state and Crawl4AI directory, removed after the run.
Crawl4AI content caching is disabled. No personal profile, session, storage
state or persistent content cache is opened, and Jina cannot run.

The browser installation locator is resolved before the temporary home replaces
the original home. `PLAYWRIGHT_BROWSERS_PATH` can select an existing installation;
relative paths use Playwright's original `INIT_CWD` or working directory.
The value `0` selects Playwright's package-local `.local-browsers` directory.
Without an explicit locator, the original standard binary cache is used:
`~/Library/Caches/ms-playwright` on macOS, `$XDG_CACHE_HOME/ms-playwright` or
`~/.cache/ms-playwright` on Linux, and `%LOCALAPPDATA%/ms-playwright` on Windows.
This locator grants access to browser binaries, not a browser profile.

The public lifecycle requires POSIX event-loop signal support, as provided on
macOS and Linux.

### Public requests and browser restrictions

A fresh HTTPX client makes each broker request, with environment trust, cookies,
authentication and automatic redirects disabled. Browser-supplied headers are
not forwarded. Only GET and HEAD are allowed. Every target is checked by the
shared public-URL policy, and URLs with userinfo are rejected.

The broker connects to a validated public IP literal, preserves the original
Host header and supplies the original HTTPS hostname through HTTPX's
`sni_hostname` extension. HTTPX verifies TLS certificates. This binds the
broker's destination to its validation result; it does not rely on a second
hostname lookup at connection time. Redirect destinations undergo the same
checks, with at most five hops. Subresource redirects are blocked, and no 3xx
response is fulfilled to Chromium.

The implementation requests offline mode and configures an exclusively bound
loopback proxy socket that never listens or forwards traffic. The descriptor
stays open through browser and driver cleanup. These settings do not establish
that native browser networking honors the policy. Browser routing supplies HTTP
responses from the broker.
Only content type, content language and CORS origin response headers survive;
Set-Cookie and active redirect or reporting headers do not.

Documents receive an opaque-origin CSP sandbox with scripts enabled. Cookie
access is disabled before page scripts run. CSP, browser hooks and configuration
restrict workers, frames, objects, popups, WebSockets, WebRTC and WebTransport.
Required hook and JavaScript capability checks fail closed. These restrictions
reduce page fidelity.
A page can fail because a required script or other subresource cannot be fetched.

This is not an airgap, an OS network sandbox or protection against a Chromium
exploit. Native-network safety and security verification gaps remain blocking.
The runtime guards and synthetic tests do not prove policy coverage for native
browser traffic. Crawl4AI's vendor launch flags include `--no-sandbox` and
`--ignore-certificate-errors`; the implementation does not remove them. The
HTTPX broker owns TLS verification. No live-source acceptance is claimed.

### Public limits and lifecycle

| Limit | `public/v1` |
|---|---|
| URL validation | 5 seconds per target; at most 8 resolver workers |
| Page navigation | 15 seconds |
| Render delay before capture | 1.5 seconds |
| Redirects | 5 |
| Broker requests | 200 per invocation |
| Response body | 5,000,000 raw bytes; only identity content encoding accepted |
| Total broker bodies | 20,000,000 raw bytes |
| Markdown | 1,000,000 characters |
| Rendered HTML | 2 MiB UTF-8 |
| JSON stdout including newline | 8 MiB |

HTML or JSON over the limit produces `too_large` with no partial document.
`--timeout` defaults to 120 seconds and covers public validation, fetching,
browser startup and rendering. Each target's DNS validation has a 5-second
limit. Either a DNS validation timeout or the global deadline produces `timeout`
and exit code 3. Cleanup has at most 10 additional seconds,
including an owned-driver stop if normal browser close fails. SIGINT and
SIGTERM cancel the task and run cleanup. SIGKILL cannot run cleanup.
Uncancellable OS DNS calls use bounded daemon workers so they cannot keep the
CLI alive after its deadline. They hold their slots until they finish or the
CLI exits.

Public stderr contains JSON stage records with a correlation ID, elapsed time,
event and typed outcome. Vendor output is suppressed. Records contain no URLs,
headers, page bodies or environment values. Callers can retain stderr to identify
the last stage reached and the cleanup outcome.

### Configured pipeline and service settings

These settings affect the default CLI, library and service, not `public/v1`.

| Variable | Default | Meaning |
|---|---|---|
| `SCRAPER_PORT` | `8020` | HTTP port |
| `SCRAPER_HOST` | `127.0.0.1` | HTTP bind address |
| `SCRAPER_API_KEY` | unset | Require Bearer or X-API-Key authentication |
| `SCRAPER_JINA_ENABLED` | `false` | Allow third-party Jina fallback |
| `SCRAPER_SERVER_TIMEOUT_SECONDS` | `120` | Service request deadline |
| `SCRAPER_MAX_CONCURRENT_SCRAPES` | `4` | Service concurrency cap |
| `SCRAPER_CRAWL_TIMEOUT_SECONDS` | `25` | Crawl4AI attempt timeout |

The default pipeline validates public addresses but does not bind its renderer's
connections to validated IPs. Its route guards do not prove coverage of every
native browser request or redirect. Its URL policy strips userinfo rather than
providing the public profile's stricter rejection contract. Enable an API key
before exposing the service beyond loopback. Jina sends the target URL to
`r.jina.ai` when enabled. Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Contributing

```bash
uv sync
uv run playwright install --with-deps chromium
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest tests/ -v
uv run --with pip-audit pip-audit --ignore-vuln PYSEC-2026-3740
```

Tests use synthetic DNS and HTTP fixtures plus real Chromium and owned loopback
sentinels. They require the browser installation and do not skip if it is absent.
No live content source or personal profile is required. CI installs Chromium on
its disposable runner. The dependency audit ignores the documented, unresolved
NLTK model-artifact advisory; the CLI does not accept model-artifact paths.

For the existing container deployment, use `docker compose up -d`. Container
runtime and exposure settings are defined by the repository's Compose file.

## License

MIT
