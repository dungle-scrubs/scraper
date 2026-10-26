"""Production CLI contract with real Chromium and synthetic external transport."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = "The amber telescope discovered seven quiet moons beyond the violet planet."
BOOTSTRAP = (
    r"""
import asyncio, atexit, json, os, sys
from pathlib import Path
import httpx
import scraper.safety as safety
safety._default_resolver = lambda host: ["93.184.216.34"]
import importlib.util
public = None
if importlib.util.find_spec("scraper.public_profile"):
    from scraper import public_profile as public
requests = []
record_path = Path(sys.argv[1])
async def respond(request):
    requests.append({"url":str(request.url), "headers":dict(request.headers),
                     "sni":request.extensions.get("sni_hostname"), "method":request.method})
    if request.url.path == "/article.json":
        body = json.dumps({"article":"The amber telescope discovered seven quiet moons beyond the violet planet."})
        content_type = "application/json"
    else:
        body = """
    + '"""'
    + """<!doctype html><html><body><h1>Observatory</h1>
        <script>fetch('/article.json').then(r=>r.json()).then(d=>{
          const a=document.createElement('article');
          const p=document.createElement('p');p.textContent=d.article;a.append(p);
          const l=document.createElement('a');l.href='/story/moons';l.textContent='Moon observation';a.append(l);
          const t=document.createElement('time');t.dateTime='2026-10-26T00:00:00Z';t.textContent='Observed today';a.append(t);
          document.body.append(a);
        });</script></body></html>"""
    + '"""'
    + """
        content_type = "text/html"
    return httpx.Response(200, headers={"content-type":content_type,"access-control-allow-origin":"*",
                                      "set-cookie":"fixture=synthetic; Secure; SameSite=None"}, content=body)
if public is not None:
    public._open_transport = lambda: httpx.MockTransport(respond)
def record():
    record_path.write_text(json.dumps({"requests":requests,"modules":[m for m in sys.modules if m.startswith("scraper.")]}))
atexit.register(record)
sys.argv = ["scraper", *sys.argv[2:]]
from scraper.cli import main
main()
"""
)


def run_cli(tmp_path, *args, bootstrap=BOOTSTRAP, extra_env=None, stdin=None):
    record = tmp_path / "boundary.json"
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH",
            "/Users/kevin/dev/scraper/.scratch/news-public-rendering/evidence/playwright-browsers",
        ),
        **(extra_env or {}),
    }
    completed = subprocess.run(
        [sys.executable, "-c", bootstrap, str(record), "scrape", "--profile", "public", *args],
        env=env,
        cwd=tmp_path,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=40,
    )
    return completed, json.loads(record.read_text()) if record.exists() else {}


def test_public_cli_returns_javascript_article_html_and_markdown(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/index")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["profile"] == "public/v1"
    assert result["ok"] is True and result["failure"] is None
    assert ARTICLE in result["markdown"]
    assert f"<p>{ARTICLE}</p>" in result["html"]
    assert '<a href="/story/moons">Moon observation</a>' in result["html"]
    assert '<time datetime="2026-10-26T00:00:00Z">' in result["html"]
    assert len(boundary["requests"]) == 2
    assert "scraper.settings" not in boundary["modules"]
    assert "scraper.scraper" not in boundary["modules"]
