# dendrite-scraper

Web scraping service with anti-bot detection, Jina fallback, and optional
model cleanup. Runs as a standalone service or installs as a Python package.

## Install

```bash
pip install dendrite-scraper
```

## API

```
POST /scrape {"url": "https://example.com"}
GET  /health
```

### Response

```json
{
  "markdown": "# Page Title\n\nClean content...\n",
  "source": "crawl4ai",
  "url": "https://example.com",
  "bot_detected": false,
  "llm_cleaned": false,
  "error": null,
  "elapsed_ms": 1234.5,
  "attempts": ["crawl4ai attempt 1"]
}
```

## Pipeline

1. **Crawl4AI** — local Playwright headless Chromium, retries on transient
   errors
2. **Bot detection** — Cloudflare/CAPTCHA phrases + partial JS render
   heuristic (empty table cells)
3. **Jina Reader fallback** — free cloud re-fetch when bot-blocked or crawl
   fails
4. **Noise detection** — link-density heuristic for nav/sidebar chrome
5. **Model cleanup** — optional EmberLM local-model pass to strip
   non-content noise from noisy markdown
6. **Artifact stripping** — regex patterns for "Skip to content", GitHub
   chrome, duplicate lines

## CLI

The `dendrite-scraper` command exposes two subcommands:

```bash
dendrite-scraper scrape <url>       # scrape a URL, JSON on stdout
dendrite-scraper scrape --stdin     # read {"url": "..."} from stdin
dendrite-scraper serve              # start the HTTP server
```

### JSON output (stdout)

Every `scrape` invocation writes exactly one JSON object to stdout:

```json
{
  "ok": true,
  "markdown": "# Page Title\n\nClean content...\n",
  "source": "crawl4ai",
  "url": "https://example.com",
  "bot_detected": false,
  "llm_cleaned": false,
  "error": null,
  "elapsed_ms": 1234.5,
  "attempts": ["crawl4ai attempt 1"]
}
```

On failure, `ok` is `false` and `error` contains the reason. The structure
is always the same — callers can unconditionally `json.loads(stdout)`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success — content scraped |
| 1 | Scrape failed — all backends exhausted |
| 2 | Invalid input — bad URL or malformed stdin JSON |
| 3 | Timeout — global deadline exceeded |
| 4 | Internal error — unexpected crash |

### Timeout

The `--timeout` flag (default: 120s) wraps the entire pipeline in a
global deadline. When exceeded, the process exits with code 3 and the
JSON `error` field explains what happened.

```bash
dendrite-scraper scrape --timeout 30 https://slow-site.example.com
```

### stdin mode

Pipe a JSON object with a `url` field:

```bash
echo '{"url": "https://example.com"}' | dendrite-scraper scrape --stdin
```

All logging goes to stderr. stdout is exclusively for the JSON result.

## Run locally

```bash
uv sync
uv run dendrite-scraper serve
```

## Run with Docker

```bash
docker compose up -d
```

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `DENDRITE_CLEANUP_PROVIDER` | No | Cleanup backend: `auto`, `none`, or `emberlm` (default: `auto`) |
| `DENDRITE_EMBERLM_PROVIDER` | No | EmberLM provider for local cleanup (default: `mlx`) |
| `DENDRITE_EMBERLM_MODEL` | No | EmberLM model for local cleanup (default: `mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit`) |
| `DENDRITE_EMBERLM_DAEMON_URL` | No | EmberLM daemon base URL (default: `http://127.0.0.1:17412`) |
| `DENDRITE_PORT` | No | Server port (default: 8020) |
| `DENDRITE_HOST` | No | Bind address (default: `127.0.0.1`, local only; set `0.0.0.0` to expose) |
| `DENDRITE_API_KEY` | No | When set, `POST /scrape` requires a matching `Authorization: Bearer` or `X-API-Key` header |
| `DENDRITE_JINA_ENABLED` | No | Enable the third-party Jina Reader fallback (default: `false`) |
| `DENDRITE_SERVER_TIMEOUT_SECONDS` | No | Global per-request deadline for `/scrape` (default: 120) |
| `DENDRITE_MAX_CONCURRENT_SCRAPES` | No | Max concurrent scrapes before `503` (default: 4) |
| `DENDRITE_CRAWL_TIMEOUT_SECONDS` | No | Crawl4AI timeout (default: 25) |

`auto` uses EmberLM when `DENDRITE_EMBERLM_PROVIDER` and
`DENDRITE_EMBERLM_MODEL` are configured; otherwise cleanup is skipped. Set
`DENDRITE_CLEANUP_PROVIDER=none` to disable model cleanup entirely.

EmberLM cleanup requires a running EmberLM daemon (`~/dev/emberlm`) reachable at
`DENDRITE_EMBERLM_DAEMON_URL`. dendrite warms the model (`POST /v1/warm`) and
then calls its OpenAI-compatible chat endpoint directly — no EmberLM client
library needed. If the daemon is unreachable, cleanup is skipped (a warning is
logged) and the raw scraped markdown is returned.

## Security

This service fetches **arbitrary, caller-supplied URLs** and treats every
inbound URL and every fetched page as untrusted. Defaults are tuned for a
**local, single-user** tool:

- **SSRF guard.** Every URL — on the crawl4ai path, the Jina path, the CLI, and
  every redirect hop — is validated by a single guard that allows only
  `http(s)` and rejects hosts resolving to loopback, private, link-local,
  reserved, multicast, CGNAT, or cloud-metadata addresses (e.g.
  `127.0.0.1`, `169.254.169.254`, `10.0.0.0/8`). Credentials in
  `user:pass@host` URLs are stripped before any request.
- **Local by default.** The server binds `127.0.0.1`. Set `DENDRITE_HOST=0.0.0.0`
  to expose it — and set `DENDRITE_API_KEY` when you do.
- **Optional auth.** With `DENDRITE_API_KEY` set, `POST /scrape` requires a
  matching `Authorization: Bearer <key>` or `X-API-Key: <key>` header.
- **Resource bounds.** A global per-request deadline, a concurrency cap
  (`503` when exceeded), and response/markdown size caps protect against
  hangs and memory exhaustion.
- **Third-party egress is opt-in.** The Jina Reader fallback is disabled by
  default (`DENDRITE_JINA_ENABLED=false`) because it sends the target URL to a
  third party (`r.jina.ai`).

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## Use as a library

```python
from dendrite_scraper.scraper import scrape

result = await scrape("https://example.com")
print(result.markdown)
```

## Use as a service

All consumers need one env var:

```bash
DENDRITE_SCRAPER_URL=http://localhost:8020
```

```python
import httpx

resp = httpx.post(f"{DENDRITE_SCRAPER_URL}/scrape", json={"url": url})
data = resp.json()
markdown = data["markdown"]
```

## Tests

```bash
uv run pytest tests/ -v
```

30 tests, all mocked — no network required.

## Port

8020 (registered in the shared port table).

## License

MIT
