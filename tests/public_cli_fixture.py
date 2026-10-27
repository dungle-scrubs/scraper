"""Synthetic CLI driver. Only DNS, HTTP transport and explicit capability faults vary."""

import asyncio
import atexit
import contextlib
import json
import re
import socket
import sys
import threading
import time
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
ENCODED_STREAM_READ = False
ENGINES = set()
NATIVE_JARS = []
NAVIGATIONS = []
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


def capture_processes():
    for process in psutil.Process().children(recursive=True):
        with contextlib.suppress(psutil.Error):
            if process.pid in PROCESSES:
                continue
            PROCESSES[process.pid] = process.create_time()
            args = process.cmdline()
            if "-juggler-pipe" in args:
                ENGINES.add("firefox")
            if "--remote-debugging-pipe" in args and not any(a.startswith("--type=") for a in args):
                ENGINES.add("chromium")


def observe_processes():
    while not STOP.wait(0.01):
        capture_processes()


def record():
    if Path.cwd().name == "cwd" and Path.cwd().parent.name.startswith("scraper-public-"):
        RUNTIMES.add(str(Path.cwd().parent))
    remaining = []
    for pid, created in PROCESSES.copy().items():
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
                "encoded_stream_read": ENCODED_STREAM_READ,
                "engines": sorted(ENGINES),
                "navigations": NAVIGATIONS,
                "native_jars": NATIVE_JARS,
                "observed_processes": len(PROCESSES),
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

OBSERVER = threading.Thread(target=observe_processes, daemon=True)
THREADS.append(OBSERVER)
OBSERVER.start()
original_chdir = public.os.chdir


def observe_chdir(path):
    original_chdir(path)
    record()


patch.object(public.os, "chdir", observe_chdir).start()
from playwright.async_api import BrowserContext, Page  # noqa: E402

context_cookies = BrowserContext.cookies


async def observe_cookies(self, *args, **kwargs):
    cookies = await context_cookies(self, *args, **kwargs)
    NATIVE_JARS.append(len(cookies))
    record()
    return cookies


patch.object(BrowserContext, "cookies", observe_cookies).start()

page_goto = Page.goto


async def observe_goto(self, url, *args, **kwargs):
    entry = {"url": url, "outcome": "pending"}
    NAVIGATIONS.append(entry)
    record()
    try:
        result = await page_goto(self, url, *args, **kwargs)
        entry["outcome"] = "ok"
        return result
    except Exception as exc:
        entry["outcome"] = type(exc).__name__
        entry["native_errors"] = re.findall(
            r"\b(?:NS_ERROR_[A-Z_]+|NS_BINDING_[A-Z_]+)\b", str(exc)
        )
        raise
    finally:
        entry["final_url"] = self.url
        record()


patch.object(Page, "goto", observe_goto).start()

if "https://fixture.test/policy" in ARGS:
    from playwright.async_api import async_playwright

    async def native_control():
        async with async_playwright() as playwright:
            browser = await playwright.firefox.launch(headless=True, proxy={"server": TCP_URL})
            try:
                page = await browser.new_page()
                with contextlib.suppress(Exception):
                    await page.goto(TCP_URL, timeout=2000)
                await page.close()
                page = await browser.new_page()
                await page.route(
                    "https://control.invalid/",
                    lambda route: route.fulfill(
                        status=200,
                        headers={"set-cookie": "control_response=synthetic; Secure; SameSite=None"},
                        body="<!doctype html><html><body>Native positive control</body></html>",
                    ),
                )
                await page.goto("https://control.invalid/")
                capabilities = await page.evaluate("""() => {
                    document.cookie='control_script=synthetic; Secure; SameSite=None';
                    return {secure:isSecureContext,rtc:typeof RTCPeerConnection,
                            transport:typeof WebTransport,cookie:document.cookie};
                }""")
                capabilities["jar"] = len(await page.context.cookies())
            finally:
                await browser.close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"synthetic-control", UDP.getsockname())
        for _ in range(100):
            if COUNTS["tcp"] and COUNTS["udp"]:
                break
            await asyncio.sleep(0.01)
        return {**COUNTS, **capabilities}

    CONTROL = asyncio.run(native_control())
    COUNTS.update(tcp=0, udp=0)
    NATIVE_JARS.clear()
    NAVIGATIONS.clear()


def resolve(host):
    if host == "dualstack.test":
        return ["2606:4700:4700::1111", "93.184.216.34"]
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
 try{document.cookie='script=synthetic; Secure; SameSite=None';report.push('cookie='+document.cookie)}
 catch(e){report.push('cookie='+e.name)}
 for(const key of ['RTCPeerConnection','webkitRTCPeerConnection','WebTransport']) {
   report.push(key+'='+typeof window[key]);
 }
 try{localStorage.setItem('probe','synthetic');report.push('storage=unexpected')}catch(e){report.push('storage='+e.name)}
 document.addEventListener('securitypolicyviolation',e=>{
   if(e.violatedDirective==='worker-src')report.push('worker-csp=blocked');
 });
 try{await new Promise(resolve=>{
   const w=new Worker('/worker.js');
   w.onerror=()=>{report.push('worker=blocked');resolve()};
   w.onmessage=()=>{report.push('worker=unexpected');resolve()};
   setTimeout(()=>{report.push('worker=deadline');resolve()},500);
 })}catch(e){report.push('worker='+e.name)}
 document.body.dataset.policyPhase='sw';
 try{await navigator.serviceWorker.register('/sw.js');report.push('sw=unexpected')}catch(e){report.push('sw='+e.name)}
 document.body.dataset.policyPhase='realm';
 try{let f=document.createElement('iframe');document.body.append(f);void f.contentWindow.RTCPeerConnection;report.push('realm=unexpected')}catch(e){report.push('realm='+e.name)}
 document.body.dataset.policyPhase='popup';
 try{report.push('popup='+(window.open('about:blank')===null))}catch(e){report.push('popup='+e.name)}
 document.body.dataset.policyPhase='auth';
 await fetch('/auth.json',{credentials:'include',headers:{Authorization:'Bearer synthetic','Proxy-Authorization':'Basic synthetic'}}).catch(()=>{});
 document.body.dataset.policyPhase='xhr';
 await new Promise(resolve=>{const x=new XMLHttpRequest();x.open('GET','/xhr.json');x.withCredentials=true;
   x.setRequestHeader('Authorization','Bearer synthetic-xhr');x.onload=resolve;x.onerror=resolve;x.send()});
 document.body.dataset.policyPhase='post';
 await fetch('/post',{method:'POST',body:'synthetic'}).then(()=>report.push('post=unexpected')).catch(()=>report.push('post=blocked'));
 await fetch('/asset-redirect').then(()=>report.push('redirect=unexpected')).catch(()=>report.push('redirect=blocked'));
 await new Promise(resolve=>{const ws=new WebSocket('WS_URL');ws.onclose=()=>{report.push('websocket=closed');resolve()};ws.onerror=()=>{};setTimeout(resolve,800)});
 document.body.dataset.policyPhase='complete';
 const p=document.createElement('p');p.id='policy';p.textContent=report.join('|');document.body.append(p);
})();""".replace("WS_URL", WS_URL)


class EncodingTrap(httpx.AsyncByteStream):
    async def __aiter__(self):
        global ENCODED_STREAM_READ
        ENCODED_STREAM_READ = True
        record()
        raise RuntimeError("synthetic decoder stream must not be read")
        yield b""  # pragma: no cover


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
    if request.url.host == "2606:4700:4700::1111":
        raise httpx.ConnectError("synthetic unavailable IPv6", request=request)
    path = request.url.path
    headers = {
        "content-type": "text/html",
        "set-cookie": "response=synthetic; Secure; SameSite=None",
    }
    if path == "/encoding-trap":
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "br"},
            stream=EncodingTrap(),
        )
    status = 200
    body = (
        "<!doctype html><html><head></head><body><h1>Observatory</h1><script>"
        + SCRIPT
        + "</script></body></html>"
    )
    if path.endswith(".json"):
        headers["content-type"] = "application/json"
        body = json.dumps({"article": ARTICLE})
    if path == "/slow-close":
        await asyncio.sleep(12)
    if path == "/policy":
        head = "".join(f'<link rel="{rel}" href="{TCP_URL}/{rel}">' for rel in ("preconnect",))
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
    elif path == "/replace-document":
        body = body.replace(
            SCRIPT,
            "setTimeout(()=>{document.open();document.write('<html><body>Replacement</body></html>');document.close()},100);"
            + SCRIPT,
        )
    elif path == "/navigate":
        body = body.replace(
            SCRIPT, "setTimeout(()=>location.href='https://other.test/new-document',100);" + SCRIPT
        )
    elif path == "/cors":
        body = body.replace("</body>", '<script type="module" src="/module.js"></script></body>')
    elif path == "/module.js":
        headers["content-type"] = "application/javascript"
        body = """const a=await fetch('/article.json',{credentials:'include'}).then(r=>r.json());
        const b=await new Promise((resolve,reject)=>{const x=new XMLHttpRequest();x.open('GET','/xhr.json');
          x.withCredentials=true;x.onload=()=>resolve(JSON.parse(x.responseText));x.onerror=reject;x.send()});
        const p=document.createElement('p');p.id='module';p.textContent='Module fetch and XHR: '+a.article+' '+b.article;document.body.append(p);"""
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
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body.encode()))


patch.object(public, "_open_transport", lambda: httpx.MockTransport(respond)).start()

# Narrow faults do not prove support. They prove the public path refuses a lost capability.
if "https://fixture.test/fault-runtime" in ARGS:
    version = public.importlib.metadata.version
    patch.object(
        public.importlib.metadata,
        "version",
        lambda name: "0.9.1" if name == "crawl4ai" else version(name),
    ).start()
HOOK_FAULTS = {
    "https://fixture.test/fault-hook": "on_page_context_created",
    "https://fixture.test/fault-inspect": "before_retrieve_html",
    "https://fixture.test/fault-before-goto": "before_goto",
    "https://fixture.test/fault-identify": "after_goto",
    "https://fixture.test/fault-capture": "before_return_html",
}
if any(url in ARGS for url in HOOK_FAULTS):
    # Real browser and constructors remain in use; omit only the required hook.
    from crawl4ai.async_crawler_strategy import AsyncPlaywrightCrawlerStrategy

    set_hook = AsyncPlaywrightCrawlerStrategy.set_hook

    def omit_hook(self, name, hook):
        omitted = next(hook_name for url, hook_name in HOOK_FAULTS.items() if url in ARGS)
        if name != omitted:
            return set_hook(self, name, hook)

    patch.object(AsyncPlaywrightCrawlerStrategy, "set_hook", omit_hook).start()

if "https://fixture.test/slow-close" in ARGS:
    from playwright.async_api import Browser

    original_close = Browser.close

    async def slow_close(self, *args, **kwargs):
        # Finish just after the 20s operation deadline, within cleanup's grace.
        await asyncio.sleep(max(0, 21 - (time.monotonic() - CLI_STARTED)))
        await original_close(self, *args, **kwargs)

    patch.object(Browser, "close", slow_close).start()

if "https://fixture.test/fault-capture-navigation" in ARGS:
    page_content = Page.content

    async def navigate_after_content(self, *args, **kwargs):
        html = await page_content(self, *args, **kwargs)
        if self.url == "https://fixture.test/fault-capture-navigation":
            await self.goto("about:blank")
        return html

    patch.object(Page, "content", navigate_after_content).start()

if "https://fixture.test/fault-close" in ARGS:
    from playwright.async_api import Browser

    async def fail_close(_self, *_args, **_kwargs):
        raise RuntimeError("synthetic close failure")

    patch.object(Browser, "close", fail_close).start()

if "https://fixture.test/fault-websocket" in ARGS:

    async def omit_websocket(_self, *_args, **_kwargs):
        pass

    patch.object(BrowserContext, "route_web_socket", omit_websocket).start()

if any(url.startswith("https://fixture.test/fault-policy-") for url in ARGS):
    from playwright.async_api import BrowserType

    launch = BrowserType.launch

    async def policy_fault(self, *args, **kwargs):
        policy_path = Path(public.os.environ["PLAYWRIGHT_FIREFOX_POLICIES_JSON"])
        fault = next(url.rsplit("fault-policy-", 1)[1] for url in ARGS if "fault-policy-" in url)
        if fault == "ignored":
            kwargs["env"] = {
                key: value
                for key, value in public.os.environ.items()
                if key != "PLAYWRIGHT_FIREFOX_POLICIES_JSON"
            }
        elif fault == "missing":
            policy_path.unlink()
        elif fault == "malformed":
            policy_path.write_text("{")
        else:
            policy = json.loads(policy_path.read_text())
            if fault == "cookies":
                del policy["policies"]["Cookies"]
            else:
                del policy["policies"]["Preferences"][fault]
            policy_path.write_text(json.dumps(policy))
        return await launch(self, *args, **kwargs)

    patch.object(BrowserType, "launch", policy_fault).start()

sys.argv = ["scraper", *ARGS]
from scraper.cli import main  # noqa: E402

CLI_STARTED = time.monotonic()
main()
