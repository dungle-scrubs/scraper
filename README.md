# scraper

Get cleaned Markdown for the tools that collect your pages.

Use the CLI, Python library or HTTP service to render pages with Crawl4AI,
remove scraper artifacts and detect bot challenges. The CLI's `public` profile
also returns rendered HTML and enforces an anonymous, public-address policy.
For example, it rejects loopback targets before launching a browser:

```console
$ scraper scrape --profile public http://127.0.0.1/
{"ok": false, "profile": "public/v1", "failure": "url_rejected", "error": "url_rejected", ...}
```

## Install

Python 3.12 or newer is required. For `public/v1`, run these commands from this
source checkout, which contains `pyproject.toml` and `uv.lock`:

```bash
uv sync --frozen
uv run --frozen playwright install chromium firefox
uv run --frozen scraper scrape --help
```

The checkout lock selects Crawl4AI 0.9.0 and Playwright 1.60.0. That Playwright
installation supplies Firefox 150.0.2 for the public profile and Chromium for
the configured pipeline. The public runtime guard rejects other Firefox or SDK
versions with `enforcement_unavailable`. On Linux, use
`uv run --frozen playwright install --with-deps chromium firefox` for system dependencies.
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
response is fulfilled to the browser.

Public mode runs dedicated, nonpersistent Firefox online, behind an exclusively
bound loopback proxy socket that never listens or forwards traffic. Playwright
sets the Firefox browser proxy with an empty bypass list; localhost proxying is
locked on. The socket stays open through browser and driver cleanup. Browser
routes fulfill requests from the broker or abort them. The proxy refuses HTTP,
HTTPS, and WebSocket connections that do not receive a routed response.
Proxy confinement relies on the pinned browser source and bounded synthetic
observations. Runtime readiness does not observe every native traffic channel.
Only content type, content language and the profile's CORS response headers
survive; Set-Cookie and active redirect or reporting headers do not.

After process isolation, the profile writes its own Firefox policy file before
starting the driver. It locks off RTC, WebTransport, HTTP/3, DNS prefetch, link
prefetch, the predictor, and speculative connections. The native Cookies policy
rejects ordinary and private-browsing cookies. The pinned Playwright Firefox
configuration also disables background connectivity checks and telemetry.
An inherited policy path cannot select this file. Higher-priority Linux system
or distribution policies and macOS or Windows enterprise policies may override
the owned file. The supported profile assumes an unmanaged, trusted browser and
host.

Before product navigation, a trusted secure document checks native RTC and
WebTransport absence, response and script cookie rejection, the empty cookie
jar, and actual WebSocket route closure. The whole owned policy file must match
the required settings. A missing file, partial file, failed readiness check, or
missing required hook returns `enforcement_unavailable`. These checks do not
inspect every effective native preference or lock; those controls also rely on
the pinned native policy implementation on that supported host. Readiness
observes only the effects listed above, not every effective preference or lock.

Documents receive an opaque-origin CSP sandbox with scripts enabled. CSP
restricts workers, Service Workers, frames, objects, and popups. WebSocket routes
close connections. Capture retains a native DOM root handle and checks its
attachment and main-frame navigation events before and after HTML retrieval.
A replacement document, extra navigation, or changed result fails closed.
Capture also rejects same-document hash, history, path, or query changes.
These strict URL checks may reject single-page applications.
These restrictions reduce page fidelity. A required subresource that cannot be
fetched can fail the page.

Synthetic CLI tests exercise these boundaries with an owned TCP listener and
UDP receiver. They do not establish packet-level absence of all browser traffic,
an OS sandbox, or protection against browser exploits. Crawl4AI's vendor launch
arguments remain in place; HTTPX owns broker TLS verification. Independent
security review and source-access clearance remain required before merge.
No live-source acceptance is claimed.

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
the last stage reached and the cleanup outcome. An enclosing deadline can
cancel an inner stage, which records `cancelled`; the final public end record
and result report `timeout`.

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
uv run playwright install --with-deps firefox
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest tests/ -v
uv run --with pip-audit pip-audit --ignore-vuln PYSEC-2026-3740
```

Tests use synthetic DNS and HTTP fixtures plus real Firefox and owned loopback
sentinels. They require the browser installation and do not skip if it is absent.
No live content source or personal profile is required. CI installs Firefox on
its disposable runner. The dependency audit ignores the documented, unresolved
NLTK model-artifact advisory; the CLI does not accept model-artifact paths.

For the existing container deployment, use `docker compose up -d`. Container
runtime and exposure settings are defined by the repository's Compose file.

## License

MIT
