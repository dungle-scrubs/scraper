"""Synthetic CLI driver. Only DNS, HTTP transport and explicit capability faults vary."""

import asyncio
import atexit
import contextlib
import json
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import psutil

import scraper.safety as safety
from scraper import public_profile as public

RECORD = Path(sys.argv[1])
ARGS = sys.argv[2:]
REQUESTS = []
RUNTIMES = set()
PROCESSES = {}
COUNTS = {"tcp": 0, "udp": 0}
CONTROL = None
LAUNCH_CHECKS = []
STOP = threading.Event()
TCP = socket.socket()
TCP.bind(("127.0.0.1", 0))
TCP.listen()
TCP.settimeout(0.1)
UDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
UDP.bind(("127.0.0.1", 0))
UDP.settimeout(0.1)
TCP_URL = f"http://127.0.0.1:{TCP.getsockname()[1]}"
WS_URL = f"ws://127.0.0.1:{TCP.getsockname()[1]}"
UDP_PORT = UDP.getsockname()[1]


def receive_tcp():
    while not STOP.is_set():
        try:
            connection, _ = TCP.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        COUNTS["tcp"] += 1
        connection.close()


def receive_udp():
    while not STOP.is_set():
        try:
            UDP.recvfrom(4096)
        except TimeoutError:
            continue
        except OSError:
            return
        COUNTS["udp"] += 1


THREADS = [threading.Thread(target=f, daemon=True) for f in (receive_tcp, receive_udp)]
for thread in THREADS:
    thread.start()


def record():
    if Path.cwd().name == "cwd":
        RUNTIMES.add(str(Path.cwd().parent))
    for process in psutil.Process().children(recursive=True):
        with contextlib.suppress(psutil.Error):
            PROCESSES[process.pid] = process.create_time()
            args = process.cmdline()
            if "--remote-debugging-pipe" in args and not any(a.startswith("--type=") for a in args):
                LAUNCH_CHECKS.append(
                    {
                        "proxy": any(
                            a.startswith("--proxy-server=http://127.0.0.1:") for a in args
                        ),
                        "loopback": "--proxy-bypass-list=<-loopback>" in args,
                        "no_direct": not any(
                            "direct://" in a.lower()
                            or a.startswith("--proxy-pac-url=")
                            or a == "--proxy-auto-detect"
                            for a in args
                        ),
                    }
                )
    remaining = []
    for pid, created in PROCESSES.items():
        try:
            if psutil.Process(pid).create_time() == created:
                remaining.append(pid)
        except psutil.Error:
            pass
    RECORD.write_text(
        json.dumps(
            {
                "requests": REQUESTS,
                "modules": [m for m in sys.modules if m.startswith("scraper.")],
                "runtimes": sorted(RUNTIMES),
                "remaining": remaining,
                "counts": COUNTS,
                "control": CONTROL,
                "launch_checks": LAUNCH_CHECKS,
            }
        )
    )


def finish():
    STOP.set()
    TCP.close()
    UDP.close()
    for thread in THREADS:
        thread.join(0.5)
    record()


atexit.register(finish)

if "https://fixture.test/policy" in ARGS:
    from playwright.async_api import async_playwright

    async def native_control():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                with contextlib.suppress(Exception):
                    await page.goto(TCP_URL, timeout=2000)
            finally:
                await browser.close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"synthetic-control", UDP.getsockname())
        for _ in range(100):
            if COUNTS["tcp"] and COUNTS["udp"]:
                break
            await asyncio.sleep(0.01)
        return dict(COUNTS)

    CONTROL = asyncio.run(native_control())
    COUNTS.update(tcp=0, udp=0)


def resolve(host):
    if host == "slow-dns.test":
        # This is an external resolver fault, not a replacement policy outcome.
        import time

        time.sleep(20)
    return ["93.184.216.34"]


patch.object(safety, "_default_resolver", resolve).start()

ARTICLE = "The amber telescope discovered seven quiet moons beyond the violet planet."
SCRIPT = """document.body.dataset.fragment=location.hash;
fetch('/article.json').then(r=>r.json()).then(d=>{
 const a=document.createElement('article');
 const p=document.createElement('p');p.textContent=d.article;a.append(p);
 const l=document.createElement('a');l.href='/story/moons';l.textContent='Moon observation';a.append(l);
 const t=document.createElement('time');t.dateTime='2026-10-26T00:00:00Z';t.textContent='Observed today';a.append(t);
 document.body.append(a);
});"""
POLICY = """(async()=>{
 const report=[];
 document.cookie='script=synthetic; Secure; SameSite=None';
 report.push('cookie='+document.cookie, 'cookieStore='+typeof cookieStore);
 for(const key of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport']) {
   let blocked=false;try{Object.defineProperty(window,key,{value:function(){}})}catch(e){blocked=true}
   report.push(key+'='+typeof window[key]+':'+blocked);
 }
 try{localStorage.setItem('probe','synthetic');report.push('storage=unexpected')}catch(e){report.push('storage='+e.name)}
 try{new Worker('/worker.js');report.push('worker=unexpected')}catch(e){report.push('worker='+e.name)}
 try{await navigator.serviceWorker.register('/sw.js');report.push('sw=unexpected')}catch(e){report.push('sw='+e.name)}
 try{let f=document.createElement('iframe');document.body.append(f);void f.contentWindow.RTCPeerConnection;report.push('realm=unexpected')}catch(e){report.push('realm='+e.name)}
 report.push('popup='+(window.open('about:blank')===null));
 await fetch('/auth.json',{credentials:'include',headers:{Authorization:'Bearer synthetic','Proxy-Authorization':'Basic synthetic'}}).catch(()=>{});
 await new Promise(resolve=>{const x=new XMLHttpRequest();x.open('GET','/xhr.json');x.withCredentials=true;
   x.setRequestHeader('Authorization','Bearer synthetic-xhr');x.onload=resolve;x.onerror=resolve;x.send()});
 await fetch('/post',{method:'POST',body:'synthetic'}).then(()=>report.push('post=unexpected')).catch(()=>report.push('post=blocked'));
 await fetch('/asset-redirect').then(()=>report.push('redirect=unexpected')).catch(()=>report.push('redirect=blocked'));
 await new Promise(resolve=>{const ws=new WebSocket('WS_URL');ws.onclose=()=>{report.push('websocket=closed');resolve()};ws.onerror=()=>{};setTimeout(resolve,800)});
 const p=document.createElement('p');p.id='policy';p.textContent=report.join('|');document.body.append(p);
})();""".replace("WS_URL", WS_URL)


async def respond(request):
    REQUESTS.append(
        {
            "url": str(request.url),
            "headers": dict(request.headers),
            "sni": request.extensions.get("sni_hostname"),
            "method": request.method,
        }
    )
    record()
    path = request.url.path
    headers = {
        "content-type": "text/html",
        "access-control-allow-origin": "*",
        "set-cookie": "response=synthetic; Secure; SameSite=None",
    }
    status = 200
    body = (
        "<!doctype html><html><head></head><body><h1>Observatory</h1><script>"
        + SCRIPT
        + "</script></body></html>"
    )
    if path.endswith(".json"):
        headers["content-type"] = "application/json"
        body = json.dumps({"article": ARTICLE})
    if path == "/policy":
        head = "".join(
            f'<link rel="{rel}" href="{TCP_URL}/{rel}">' for rel in ("preconnect", "dns-prefetch")
        )
        body = body.replace("</head>", head + "</head>").replace(
            "</body>",
            '<iframe src="/frame"></iframe><object data="/object"></object><script>'
            + POLICY
            + "</script></body>",
        )
    elif path == "/refresh":
        body = body.replace(
            "</head>", f'<meta http-equiv="refresh" content="0;url={TCP_URL}/refresh"></head>'
        )
    elif path == "/unsafe-asset":
        body = body.replace("</body>", '<img src="http://169.254.169.254/metadata"></body>')
    elif path == "/slow.js":
        await asyncio.sleep(30)
    elif path == "/slow":
        body = body.replace("</head>", '<script src="/slow.js"></script></head>')
    elif path == "/big-markdown":
        body = body.replace(SCRIPT, "document.body.append('x'.repeat(1000100));")
    elif path == "/big-html":
        body = body.replace(SCRIPT, "document.body.append('x'.repeat(2097152));")
    elif path == "/big-json":
        body = body.replace(SCRIPT, "document.body.append('\\u0001'.repeat(1500000));")
    elif path in ("/big-declared", "/big-stream"):
        if path == "/big-declared":
            headers["content-length"] = "5000001"
        else:
            body = "x" * 5_000_001
    elif path == "/bot":
        body = "<html><body><p>cf-browser-verification Please wait for the synthetic browser verification challenge to complete before proceeding.</p></body></html>"
    elif path == "/wrong-type":
        headers["content-type"] = "application/pdf"
    elif path == "/http-error":
        status = 401
    elif path == "/many":
        body = body.replace(
            SCRIPT, "for(let n=0;n<205;n++)fetch('/auth.json?n='+n).catch(()=>{});" + SCRIPT
        )
    elif path == "/crawl-limit":
        body = body.replace(
            SCRIPT, "for(let n=0;n<5;n++)fetch('/chunk?n='+n).catch(()=>{});" + SCRIPT
        )
    elif path == "/chunk":
        body = "x" * 4_500_000
    elif path in (
        "/redirect",
        "/private-redirect",
        "/credential-redirect",
        "/loop",
        "/asset-redirect",
    ):
        status = 302
        headers["location"] = {
            "/redirect": "https://other.test/index",
            "/private-redirect": "http://10.0.0.1/",
            "/credential-redirect": "https://synthetic:synthetic@other.test/",
            "/loop": "/loop",
            "/asset-redirect": TCP_URL + "/redirect-target",
        }[path]
    return httpx.Response(status, headers=headers, content=body)


patch.object(public, "_open_transport", lambda: httpx.MockTransport(respond)).start()

# Narrow faults do not prove support. They prove the public path refuses a lost capability.
if "https://fixture.test/fault-runtime" in ARGS:
    version = public.importlib.metadata.version
    patch.object(
        public.importlib.metadata,
        "version",
        lambda name: "0.9.1" if name == "crawl4ai" else version(name),
    ).start()
if "https://fixture.test/fault-hook" in ARGS:
    # Real browser and constructors remain in use; omit only the required hook.
    from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy

    set_hook = AsyncPlaywrightCrawlerStrategy.set_hook

    def omit_hook(self, name, hook):
        if name != "on_page_context_created":
            return set_hook(self, name, hook)

    patch.object(AsyncPlaywrightCrawlerStrategy, "set_hook", omit_hook).start()

if "https://fixture.test/slow-close" in ARGS:
    from playwright.async_api import Browser

    original_close = Browser.close

    async def slow_close(self, *args, **kwargs):
        await asyncio.sleep(5)
        await original_close(self, *args, **kwargs)

    patch.object(Browser, "close", slow_close).start()

if "https://fixture.test/fault-close" in ARGS:
    from playwright.async_api import Browser

    async def fail_close(_self, *_args, **_kwargs):
        raise RuntimeError("synthetic close failure")

    patch.object(Browser, "close", fail_close).start()

if "https://fixture.test/fault-init" in ARGS:
    from playwright.async_api import BrowserContext

    async def omit_init(self, *args, **kwargs):
        pass

    patch.object(BrowserContext, "add_init_script", omit_init).start()

sys.argv = ["scraper", *ARGS]
from scraper.cli import main  # noqa: E402

main()
