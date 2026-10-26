"""Production CLI contract with real Chromium and synthetic external transport."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARTICLE = "The amber telescope discovered seven quiet moons beyond the violet planet."
BOOTSTRAP = (Path(__file__).parent / "public_cli_fixture.py").read_text()


def cli_env(tmp_path, extra_env=None):
    cache = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
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
    completed = subprocess.run(
        [sys.executable, "-c", bootstrap, str(record), "scrape", "--profile", "public", *args],
        env=cli_env(tmp_path, extra_env),
        cwd=tmp_path,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=40,
    )
    boundary = json.loads(record.read_text()) if record.exists() else {}
    assert all(not Path(path).exists() for path in boundary.get("runtimes", [])), boundary
    assert boundary.get("remaining", []) == [], boundary
    return completed, boundary


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
    assert boundary["launch_checks"] and all(all(c.values()) for c in boundary["launch_checks"])


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
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert ARTICLE in json.loads(completed.stdout)["markdown"]
    assert "scraper.settings" not in boundary["modules"]
    assert "scraper.scraper" not in boundary["modules"]
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


def test_cookie_authorization_and_native_capabilities_at_actual_browser_boundary(tmp_path):
    completed, boundary = run_cli(tmp_path, "https://fixture.test/policy")
    assert completed.returncode == 0, completed.stderr
    html = json.loads(completed.stdout)["html"]
    for expected in (
        "cookie=|cookieStore=undefined",
        "RTCPeerConnection=undefined:true",
        "webkitRTCPeerConnection=undefined:true",
        "WebTransport=undefined:true",
        "storage=SecurityError",
        "worker=SecurityError",
        "sw=SecurityError",
        "realm=SecurityError",
        "popup=true",
        "post=blocked",
        "redirect=blocked",
        "websocket=closed",
    ):
        assert expected in html
    assert boundary["control"]["tcp"] > 0 and boundary["control"]["udp"] > 0
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
        ("https://fixture.test/refresh", "url_rejected", 2),
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


@pytest.mark.parametrize("path", ["fault-runtime", "fault-hook", "fault-init"])
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
    completed, boundary = run_cli(tmp_path, "https://fixture.test/slow", "--timeout", "4")
    assert completed.returncode == 3, completed.stderr
    assert json.loads(completed.stdout)["failure"] == "timeout"
    assert time.monotonic() - start < 15
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
    completed, _boundary = run_cli(tmp_path, "https://fixture.test/slow-close", "--timeout", "5")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True
    events = [json.loads(line) for line in completed.stderr.splitlines()]
    assert next(e["elapsed_ms"] for e in events if e["stage"] == "cleanup") < 5000
    assert events[-1]["elapsed_ms"] > 5000


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
