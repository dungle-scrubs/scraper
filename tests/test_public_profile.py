"""Production CLI contract with real Firefox and synthetic external transport."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = "The amber telescope discovered seven quiet moons beyond the violet planet."
BOOTSTRAP = (Path(__file__).parent / "public_cli_fixture.py").read_text()


def cli_env(tmp_path, extra_env=None):
    cache = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    if sys.platform != "darwin":
        cache = Path(os.environ.get("XDG_CACHE_HOME") or cache)
    locator = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(cache / "ms-playwright"))
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        "PLAYWRIGHT_BROWSERS_PATH": locator,
        **(extra_env or {}),
    }


def run_cli(tmp_path, *args, bootstrap=BOOTSTRAP, extra_env=None, stdin=None):
    record = tmp_path / "boundary.json"
    command = [sys.executable, "-c", bootstrap, str(record), "scrape", "--profile", "public", *args]
    process = subprocess.Popen(
        command,
        env=cli_env(tmp_path, extra_env),
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    observed = {}
    stop = threading.Event()
    root = psutil.Process(process.pid)

    def observe():
        while not stop.is_set():
            try:
                for child in root.children(recursive=True):
                    observed[child.pid] = child.create_time()
            except psutil.Error:
                pass
            stop.wait(0.005)

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=60)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        stop.set()
        observer.join()
    (tmp_path / "stdout.json").write_text(stdout)
    (tmp_path / "stderr.jsonl").write_text(stderr)
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    remaining = []
    for pid, created in observed.items():
        try:
            child = psutil.Process(pid)
            if child.create_time() == created:
                remaining.append(child)
        except psutil.Error:
            pass
    _, remaining = psutil.wait_procs(remaining, timeout=1)
    try:
        assert not remaining, [p.pid for p in remaining]
    finally:
        for child in remaining:
            try:
                if child.create_time() == observed[child.pid]:
                    child.kill()
            except psutil.Error:
                pass

    boundary = json.loads(record.read_text()) if record.exists() else {}
    assert all(not Path(path).exists() for path in boundary.get("runtimes", [])), boundary
    assert boundary.get("remaining", []) == [], boundary
    events = [
        json.loads(line) for line in completed.stderr.splitlines() if line.startswith('{"profile":')
    ]
    if any(e["stage"] == "fetch" for e in events):
        assert boundary["runtimes"], "isolation was entered but not observed"
    if any(e["stage"] == "browser" for e in events):
        assert observed, "real-browser lifecycle was not observed externally"
        assert boundary["observed_processes"] > 0
    return completed, boundary


def test_public_cli_uses_native_firefox_for_javascript_output(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/index")
    assert completed.returncode == 0, (completed.stderr, boundary)
    result = json.loads(completed.stdout)
    assert ARTICLE in result["markdown"] and f"<p>{ARTICLE}</p>" in result["html"]
    assert boundary["engines"] == ["firefox"]
    assert boundary["counts"] == {"tcp": 0, "udp": 0}


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
    assert boundary["runtimes"]
    assert boundary["engines"] == ["firefox"]
    assert boundary["navigations"][0]["url"] == "https://public-profile.invalid/readiness"


def test_hostile_configuration_cannot_enable_settings_jina_or_browser_state(tmp_path):
    (tmp_path / ".env").write_text("SCRAPER_JINA_ENABLED=true\nSCRAPER_PORT=not-an-int\n")
    inherited_tmp = tmp_path / "ambient-tmp"
    inherited_tmp.mkdir()
    completed, boundary = run_cli(
        tmp_path,
        "https://fixture.test/index",
        extra_env={
            "SCRAPER_JINA_ENABLED": "true",
            "SCRAPER_MAX_MARKDOWN_CHARS": "invalid",
            "HTTP_PROXY": "http://synthetic:synthetic@127.0.0.1:9",
            "CDP_URL": "http://127.0.0.1:9",
            "CRAWL4_AI_BASE_DIRECTORY": str(tmp_path / "forbidden"),
            "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK": "1",
            "TMPDIR": str(inherited_tmp),
            "PLAYWRIGHT_FIREFOX_POLICIES_JSON": str(tmp_path / "not-an-owned-policy.json"),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert ARTICLE in json.loads(completed.stdout)["markdown"]
    assert "scraper.settings" not in boundary["modules"]
    assert "scraper.scraper" not in boundary["modules"]
    assert boundary["engines"] == ["firefox"]
    assert boundary["native_jars"] and set(boundary["native_jars"]) == {0}
    assert not (tmp_path / "forbidden").exists()
    assert all(not Path(p).is_relative_to(inherited_tmp) for p in boundary["runtimes"])
    assert all(r["headers"]["host"] == "fixture.test" for r in boundary["requests"])
    control = subprocess.run(
        [sys.executable, "-m", "scraper", "scrape", "https://fixture.test/index"],
        cwd=tmp_path,
        env=cli_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert control.returncode != 0
    assert "ValidationError" in control.stderr


def test_native_policy_cookie_authorization_and_csp_at_actual_browser_boundary(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/policy")
    assert completed.returncode == 0, completed.stderr
    html = json.loads(completed.stdout)["html"]
    for expected in (
        "cookie=SecurityError",
        "RTCPeerConnection=undefined",
        "webkitRTCPeerConnection=undefined",
        "WebTransport=undefined",
        "storage=SecurityError",
        "worker=blocked",
        "worker-csp=blocked",
        "sw=SecurityError",
        "realm=SecurityError",
        "popup=InvalidAccessError",
        "post=blocked",
        "redirect=blocked",
        "websocket=closed",
    ):
        assert expected in html, (expected, html[:400], html.split('<p id="policy">')[-1][:600])
    assert boundary["control"]["tcp"] > 0 and boundary["control"]["udp"] > 0
    assert boundary["control"]["secure"] is True
    assert boundary["control"]["rtc"] == "function"
    assert boundary["control"]["transport"] == "function"
    assert boundary["control"]["jar"] == 2
    assert "control_response=synthetic" in boundary["control"]["cookie"]
    assert "control_script=synthetic" in boundary["control"]["cookie"]
    assert boundary["native_jars"] and set(boundary["native_jars"]) == {0}
    assert boundary["navigations"][0]["url"] == "https://public-profile.invalid/readiness"
    assert boundary["navigations"][1]["url"] == "https://fixture.test/policy"
    requests = boundary["requests"]
    paths = {r["url"].split("93.184.216.34", 1)[-1] for r in requests}
    assert {"/auth.json", "/xhr.json", "/asset-redirect"} <= paths
    assert not {"/worker.js", "/sw.js", "/frame", "/object", "/post"} & paths
    assert boundary["counts"] == {"tcp": 0, "udp": 0}
    for request in requests:
        assert not {"cookie", "authorization", "proxy-authorization"} & request["headers"].keys()
        assert request["method"] == "GET"
        assert request["url"].startswith("https://93.184.216.34/")
        assert request["headers"]["host"] == "fixture.test"
        assert request["sni"] == "fixture.test"


@pytest.mark.parametrize(
    "url,failure,exit_status",
    [
        ("https://synthetic:synthetic@fixture.test/", "credentials_in_url", 2),
        ("https://synthetic@fixture.test/", "credentials_in_url", 2),
        ("http://127.0.0.1/", "url_rejected", 2),
        ("http://[::ffff:127.0.0.1]/", "url_rejected", 2),
        ("file:///etc/passwd", "url_rejected", 2),
        ("https://fixture.test/private-redirect", "redirect_rejected", 1),
        ("https://fixture.test/credential-redirect", "redirect_rejected", 1),
        ("https://fixture.test/loop", "redirect_rejected", 1),
        ("https://fixture.test/wrong-type", "unsupported_content", 1),
        ("https://fixture.test/http-error", "http_status", 1),
        ("https://fixture.test/bot", "bot_detected", 1),
        ("https://fixture.test/unsafe-asset", "url_rejected", 2),
        ("https://fixture.test/refresh", "navigation_rejected", 1),
    ],
)
def test_public_rejections_are_typed_and_do_not_leak_input(tmp_path, url, failure, exit_status):
    completed, boundary = run_cli(tmp_path, url)
    assert completed.returncode == exit_status, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] == failure and result["ok"] is False
    assert result["url"] == "" and result["html"] == "" and result["markdown"] == ""
    assert "synthetic:" not in completed.stdout + completed.stderr
    assert boundary["counts"] == {"tcp": 0, "udp": 0}
    assert all(
        "127.0.0.1" not in r["url"] and "10.0.0.1" not in r["url"] for r in boundary["requests"]
    )


def test_redirects_are_validated_and_preserve_host_and_tls_name(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/redirect")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["final_url"] == "https://other.test/index"
    assert [r["headers"]["host"] for r in boundary["requests"]] == [
        "fixture.test",
        "other.test",
        "other.test",
    ]
    assert [r["sni"] for r in boundary["requests"]] == ["fixture.test", "other.test", "other.test"]


@pytest.mark.parametrize(
    "path", ["big-html", "big-json", "big-declared", "big-stream", "many", "crawl-limit"]
)
def test_public_limits_fail_without_a_partial_document(tmp_path, path):
    completed, _boundary = run_cli(tmp_path, "https://fixture.test/" + path)
    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] == "too_large"
    assert not result["html"] and not result["markdown"]
    assert len(completed.stdout.encode()) <= 8 * 1024 * 1024
    assert json.loads(completed.stderr.splitlines()[-1])["outcome"] == "too_large"


@pytest.mark.parametrize(
    "path",
    [
        "fault-runtime",
        "fault-hook",
        "fault-websocket",
        "fault-inspect",
        "fault-before-goto",
        "fault-identify",
        "fault-capture",
    ],
)
def test_lost_required_capabilities_fail_closed(tmp_path, path):
    completed, _boundary = run_cli(tmp_path, "https://fixture.test/" + path)
    assert completed.returncode == 4, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "enforcement_unavailable"
    if path == "fault-runtime":
        assert _boundary["requests"] == []


@pytest.mark.parametrize("stdin", ["[]", "null", '{"url":7}', "not json", "{}"])
def test_public_stdin_errors_are_versioned_and_typed(tmp_path, stdin):
    completed, _boundary = run_cli(tmp_path, "--stdin", stdin=stdin)
    assert completed.returncode == 2, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "invalid_input"
    assert json.loads(completed.stdout)["profile"] == "public/v1"


def test_wall_timeout_closes_real_browser_and_records_stage_outcomes(tmp_path):
    start = time.monotonic()
    completed, boundary = run_cli(tmp_path, "https://fixture.test/slow", "--timeout", "15")
    assert completed.returncode == 3, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "timeout"
    assert time.monotonic() - start < 26
    assert any(r["url"].endswith("/slow.js") for r in boundary["requests"])
    events = [json.loads(line) for line in completed.stderr.splitlines()]
    assert all(
        set(e) == {"profile", "id", "stage", "event", "outcome", "elapsed_ms"} for e in events
    )
    assert all(e["elapsed_ms"] >= 0 for e in events)
    assert len({e["id"] for e in events}) == 1
    assert any(e["stage"] == "cleanup" and e["outcome"] == "ok" for e in events)
    assert events[-1]["stage"] == "public" and events[-1]["outcome"] == "timeout"


def test_sigterm_returns_typed_cancellation_and_removes_runtime(tmp_path):
    record = tmp_path / "boundary.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            BOOTSTRAP,
            str(record),
            "scrape",
            "--profile",
            "public",
            "https://fixture.test/slow",
        ],
        env=cli_env(tmp_path),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            if record.exists():
                try:
                    if any(
                        r["url"].endswith("/slow.js")
                        for r in json.loads(record.read_text())["requests"]
                    ):
                        break
                except json.JSONDecodeError:
                    pass
            time.sleep(0.02)
        else:
            pytest.fail("fixture never reached the external transport with a real browser")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 1, stderr
        assert json.loads(stdout)["failure"] == "cancelled"
        boundary = json.loads(record.read_text())
        assert boundary["runtimes"] and all(not Path(p).exists() for p in boundary["runtimes"])
        assert boundary["remaining"] == []
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_wall_timeout_includes_a_resolver_that_does_not_return(tmp_path):
    start = time.monotonic()
    completed, boundary = run_cli(tmp_path, "https://slow-dns.test/index", "--timeout", "1")
    assert completed.returncode == 3, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "timeout"
    assert time.monotonic() - start < 11
    assert boundary["requests"] == []


def test_same_url_document_replacement_during_capture_is_rejected(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/replace-document")
    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] == "navigation_rejected"
    assert not result["html"] and not result["markdown"]
    assert boundary["counts"] == {"tcp": 0, "udp": 0}


def test_navigation_after_serialization_cannot_return_stale_html(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/fault-capture-navigation")
    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] == "navigation_rejected"
    assert not result["html"] and not result["markdown"]
    assert boundary["navigations"][-1]["url"] == "about:blank"
    assert len(boundary["requests"]) == 2
    assert boundary["counts"] == {"tcp": 0, "udp": 0}


def test_missing_browser_installation_is_an_enforcement_failure(tmp_path):
    completed, _boundary = run_cli(
        tmp_path,
        "https://fixture.test/index",
        extra_env={
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "not-installed"),
        },
    )
    assert completed.returncode == 4, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "enforcement_unavailable"


@pytest.mark.parametrize("relative", [True, False])
def test_binary_locator_is_resolved_before_home_is_replaced(tmp_path, relative):
    locator = Path(cli_env(tmp_path)["PLAYWRIGHT_BROWSERS_PATH"])
    if relative:
        (tmp_path / "binaries").symlink_to(locator, target_is_directory=True)
        extra = {"PLAYWRIGHT_BROWSERS_PATH": "binaries"}
    else:
        cache = tmp_path / ("Library/Caches" if sys.platform == "darwin" else ".cache")
        cache.mkdir(parents=True)
        (cache / "ms-playwright").symlink_to(locator, target_is_directory=True)
        extra = {"PLAYWRIGHT_BROWSERS_PATH": ""}
    completed, _boundary = run_cli(tmp_path, "https://fixture.test/index", extra_env=extra)
    assert completed.returncode == 0, completed.stderr
    assert ARTICLE in json.loads(completed.stdout)["markdown"]


def test_browser_close_failure_still_releases_owned_driver_processes(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/fault-close")
    assert completed.returncode == 4, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "cleanup_failed"
    assert boundary["remaining"] == []
    assert json.loads(completed.stderr.splitlines()[-1])["outcome"] == "cleanup_failed"


@pytest.mark.parametrize(
    "url", ["https://fixture.test", "https://fixture.test/index#reading", "https://é.test/index"]
)
def test_browser_url_normalization_does_not_lose_the_initial_document(tmp_path, url):
    completed, boundary = run_cli(tmp_path, url)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert ARTICLE in result["markdown"]
    fragment = "#reading" if url.endswith("#reading") else ""
    assert f'data-fragment="{fragment}"' in result["html"]
    expected_host = "xn--9ca.test" if "é" in url else "fixture.test"
    assert all(
        r["headers"]["host"] == expected_host and r["sni"] == expected_host
        for r in boundary["requests"]
    )


def test_markdown_cap_preserves_the_complete_bounded_html(tmp_path):
    completed, _boundary = run_cli(tmp_path, "https://fixture.test/big-markdown")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert len(result["markdown"]) <= 1_000_000
    assert "x" * 1_000_100 in result["html"]


def test_cleanup_uses_its_grace_after_rendering_finishes_before_the_deadline(tmp_path):
    completed, _boundary = run_cli(tmp_path, "https://fixture.test/slow-close", "--timeout", "20")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True
    events = [json.loads(line) for line in completed.stderr.splitlines()]
    assert next(e["elapsed_ms"] for e in events if e["stage"] == "cleanup") < 20000
    assert events[-1]["elapsed_ms"] > 20000


def test_validated_ipv6_connect_failure_falls_back_to_validated_ipv4(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://dualstack.test/index")
    assert completed.returncode == 0, completed.stderr
    assert ARTICLE in json.loads(completed.stdout)["markdown"]
    assert [r["url"].split("/index")[0] for r in boundary["requests"][:2]] == [
        "https://[2606:4700:4700::1111]",
        "https://93.184.216.34",
    ]
    assert all(
        r["headers"]["host"] == "dualstack.test" and r["sni"] == "dualstack.test"
        for r in boundary["requests"]
    )


def test_broker_supplies_anonymous_cors_for_modules_fetch_and_credentialed_xhr(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/cors")
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert 'id="module">Module fetch and XHR: ' + ARTICLE + " " + ARTICLE in result["html"]
    assert "Module fetch and XHR: " + ARTICLE in result["markdown"]
    assert any(r["url"].endswith("/module.js") for r in boundary["requests"])
    assert all(
        not {"cookie", "authorization", "proxy-authorization"} & r["headers"].keys()
        for r in boundary["requests"]
    )


def test_per_target_dns_timeout_is_typed_as_timeout(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://slow-dns.test/index", "--timeout", "15")
    assert completed.returncode == 3, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "timeout"
    assert boundary["requests"] == []


def test_unexpected_encoding_is_rejected_before_reading_or_decoding_the_body(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/encoding-trap")
    assert completed.returncode == 1, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "unsupported_encoding"
    assert boundary["encoded_stream_read"] is False
    assert boundary["requests"][0]["headers"]["accept-encoding"] == "identity"


def test_public_document_navigation_fails_before_fetching_a_differently_labelled_page(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/navigate")
    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failure"] == "navigation_rejected" and not result["html"]
    assert not any(r["url"].endswith("/new-document") for r in boundary["requests"])


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "malformed",
        "cookies",
        "media.peerconnection.enabled",
        "network.webtransport.enabled",
        "network.http.http3.enable",
        "network.dns.disablePrefetch",
        "network.http.speculative-parallel-limit",
        "network.predictor.enabled",
        "network.prefetch-next",
        "network.proxy.allow_hijacking_localhost",
    ],
)
def test_missing_or_partial_native_policy_fails_before_product_navigation(tmp_path, fault):
    url = "https://fixture.test/fault-policy-" + fault
    completed, boundary = run_cli(tmp_path, url)
    assert completed.returncode == 4, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "enforcement_unavailable"
    assert all(nav["url"] != url for nav in boundary["navigations"])
    assert len(boundary["requests"]) == 1  # Only the validated document prefetch.
    assert boundary["counts"] == {"tcp": 0, "udp": 0}


def test_readiness_failure_records_the_trusted_stage_and_cleanup(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/fault-policy-ignored")
    assert completed.returncode == 4, completed.stderr
    events = [json.loads(line) for line in completed.stderr.splitlines()]
    assert [e["outcome"] for e in events if e["stage"] == "enforcement"] == [
        "pending",
        "enforcement_unavailable",
    ]
    assert [e["outcome"] for e in events if e["stage"] == "browser"] == [
        "pending",
        "enforcement_unavailable",
    ]
    assert [e["outcome"] for e in events if e["stage"] == "cleanup"] == ["pending", "ok"]
    assert all(
        nav["url"] != "https://fixture.test/fault-policy-ignored" for nav in boundary["navigations"]
    )


def test_native_readiness_refuses_an_unconsumed_policy_before_product_navigation(tmp_path):
    url = "https://fixture.test/fault-policy-ignored"
    completed, boundary = run_cli(tmp_path, url)
    assert completed.returncode == 4, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "enforcement_unavailable"
    assert [nav["url"] for nav in boundary["navigations"]] == [
        "https://public-profile.invalid/readiness"
    ]
    assert boundary["counts"] == {"tcp": 0, "udp": 0}
    assert len(boundary["requests"]) == 1
