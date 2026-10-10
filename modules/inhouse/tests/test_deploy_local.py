"""Spec 4.4, T2/T9: real Caddy + browser + endpoint, external navigation blocked.

Optional tools: CADDY_2111, CADDY_291, CHROMIUM_EXE and playwright.
Only loopback listeners are started; no deployed server or YooKassa is contacted.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import shutil

import pytest

# Owner 2026-10-10: these tests start real Caddy/Chromium and require explicit opt-in.
pytestmark = pytest.mark.skipif(
    os.environ.get("INHOUSE_LOCAL_DEPLOY_TESTS") != "1",
    reason="set INHOUSE_LOCAL_DEPLOY_TESTS=1 to start local Caddy/Chromium",
)
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from yookassa import Payment

from modules.inhouse.features.yookassa_methods import pay_endpoint, sdk_patch, token
from modules.inhouse.tests.helpers import SECRET, FakeStockService, activate, count_rows, make_deps, past, save_checkout

DEPLOY = Path(__file__).resolve().parents[1] / "features/yookassa_methods/deploy"
CADDY_2111 = os.environ.get("CADDY_2111", "")
CADDY_291 = os.environ.get("CADDY_291", "")
CHROMIUM = os.environ.get("CHROMIUM_EXE", "")
HAS_BROWSER = importlib.util.find_spec("playwright") is not None and Path(CHROMIUM).is_file()


async def _local_caddy_request(executable, config_text, tmp_path, path, *, before_request=None):
    """Real HTTP request through the deploy file; all listeners are loopback only."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    address = f"http://127.0.0.1:{port}"
    config_text = config_text.replace("LOCAL_SITE", address)
    config = tmp_path / "Caddyfile"
    config.write_text("{\n admin off\n auto_https off\n}\n" + config_text, encoding="utf-8")
    with (tmp_path / "caddy.log").open("wb") as log:
        proc = subprocess.Popen([executable, "run", "--config", str(config), "--adapter", "caddyfile"], stdout=log, stderr=log)
        try:
            async with ClientSession() as http:
                for _ in range(40):
                    try:
                        async with http.get(address + "/p/probe") as response:
                            await response.read()
                        break
                    except OSError:
                        assert proc.poll() is None
                        await asyncio.sleep(0.05)
                else:
                    pytest.fail("local Caddy failed to start within 2 seconds")
                if before_request is not None:
                    await asyncio.wait_for(before_request(), timeout=3)
                async with http.post(address + path, data={"t": "review-invalid"}) as response:
                    return response.status, response.headers.getall("Content-Type", []), await response.text()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            assert proc.poll() is not None


@pytest.mark.skipif(not Path(CADDY_291).is_file(), reason="CADDY_291 executable required")
async def test_store_payment_html_overrides_site_plaintext(tmp_path):
    """Spec T9/4.4: the bot's explanatory HTML page must be served as HTML."""
    app = web.Application()
    app.router.add_post("/inhouse/pay", lambda request: web.Response(text="Ссылка недействительна", content_type="text/html", status=400))
    async with TestServer(app) as upstream:
        snippet = (DEPLOY / "Caddyfile.store-snippet").read_text(encoding="utf-8")
        snippet = snippet.replace("172.18.0.1:3002", str(upstream.make_url("/")).rstrip("/"))
        config = "LOCAL_SITE {\n" + snippet + "\n header Content-Type text/plain\n}\n"
        status, content_types, body = await _local_caddy_request(CADDY_291, config, tmp_path, "/inhouse/pay")
        assert status == 400
        assert content_types == ["text/html; charset=utf-8"]
        assert "Ссылка недействительна" in body


@pytest.mark.skipif(not Path(CADDY_2111).is_file(), reason="CADDY_2111 executable required")
async def test_one_probe_failure_does_not_disable_payment_route(tmp_path):
    """Owner's health-check failure question: one failed probe must leave POST usable."""
    app = web.Application()
    failed = asyncio.Event()
    release = asyncio.Event()
    probes = 0

    async def ping(request):
        nonlocal probes
        probes += 1
        if probes == 1:
            return web.Response(status=204)
        if probes == 2:
            failed.set()
            return web.Response(status=503)
        await release.wait()
        return web.Response(status=204)

    app.router.add_get("/inhouse/ping", ping)
    app.router.add_post("/inhouse/pay", lambda request: web.Response(text="Ссылка недействительна", content_type="text/html", status=400))
    site = tmp_path / "site"
    shutil.copytree(DEPLOY, site)
    async with TestServer(app) as upstream:
        config = (DEPLOY / "Caddyfile.pay.omenso.net").read_text(encoding="utf-8")
        config = config.replace("pay.omenso.net {", "LOCAL_SITE {")
        config = config.replace("https://store.potok-lubvi.ru", str(upstream.make_url("/")).rstrip("/"))
        config = config.replace("/var/www/pay-omenso", site.as_posix()).replace("health_interval 30s", "health_interval 100ms")

        async def after_failure():
            await failed.wait()
            # Give Caddy time to consume the completed 503 before observing proxy routing.
            await asyncio.sleep(0.1)

        try:
            status, _, body = await _local_caddy_request(CADDY_2111, config, tmp_path, "/inhouse/pay", before_request=after_failure)
            assert status == 400
            assert "Ссылка недействительна" in body
        finally:
            release.set()


@pytest.mark.skipif(not Path(CADDY_291).is_file(), reason="CADDY_291 executable required")
def test_store_snippet_valid_in_caddy_291(tmp_path):
    config = tmp_path / "Caddyfile"
    config.write_text("store.potok-lubvi.ru {\n" + (DEPLOY / "Caddyfile.store-snippet").read_text(encoding="utf-8") + "\n}\n", encoding="utf-8")
    result = subprocess.run([CADDY_291, "adapt", "--validate", "--config", str(config), "--adapter", "caddyfile"], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.skipif(not Path(CADDY_2111).is_file() or not HAS_BROWSER, reason="CADDY_2111, CHROMIUM_EXE and playwright required")
@pytest.mark.parametrize("javascript", [True, False], ids=["auto-submit-csp", "no-js-button"])
async def test_real_pay_page_csp_and_no_js(tmp_path, db, yk_api, javascript):
    from playwright.async_api import async_playwright

    tg_id = 888000333
    service = FakeStockService()
    deps = make_deps(db, service)
    activate(deps)
    sdk_patch.install(Payment)
    await save_checkout(db, tg_id, {"required_amount": 199}, past(1))
    label = token.issue(SECRET, tg_id, "sbp", 1800)
    app = web.Application()
    app.router.add_post("/inhouse/pay", pay_endpoint.handle)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    address = f"http://127.0.0.1:{port}"
    site = tmp_path / "site"
    shutil.copytree(DEPLOY, site)
    unavailable = site / "unavailable.html"
    unavailable.write_text(unavailable.read_text(encoding="utf-8").replace("__BOT_LINK__", deps.bot_link), encoding="utf-8")
    # Tests use the loopback origin; production allowlist is not changed on disk.
    from types import SimpleNamespace
    deps.settings = SimpleNamespace(**{k: getattr(deps.settings, k) for k in dir(deps.settings) if k.isupper()})
    deps.settings.ALLOWED_ORIGINS = (address,)
    async with TestServer(app) as upstream:
        config_text = (DEPLOY / "Caddyfile.pay.omenso.net").read_text(encoding="utf-8")
        config_text = config_text.replace("pay.omenso.net {", address + " {")
        config_text = config_text.replace("https://store.potok-lubvi.ru", str(upstream.make_url("/")).rstrip("/"))
        config_text = config_text.replace("/var/www/pay-omenso", site.as_posix())
        config = tmp_path / "Caddyfile"
        config.write_text("{\n admin off\n auto_https off\n}\n" + config_text, encoding="utf-8")
        with (tmp_path / "caddy.log").open("wb") as log:
            proc = subprocess.Popen([CADDY_2111, "run", "--config", str(config), "--adapter", "caddyfile"], stdout=log, stderr=log)
            try:
                async with ClientSession() as http:
                    for attempt in range(40):
                        try:
                            async with http.get(address + "/p/probe") as response:
                                assert response.status == 200
                                assert "script-src 'self'" in response.headers["Content-Security-Policy"]
                            break
                        except OSError:
                            assert proc.poll() is None
                            await asyncio.sleep(0.05)
                    else:
                        pytest.fail("local Caddy did not start within 2 seconds")
                async with async_playwright() as p:
                    browser = await p.chromium.launch(executable_path=CHROMIUM, headless=True,
                        args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"])
                    try:
                        ctx = await browser.new_context(java_script_enabled=javascript)
                        traffic = []

                        async def intercept(route):
                            url = route.request.url
                            if len(traffic) < 16:
                                traffic.append((route.request.method, url.split("/p/")[0] + "/p/<test>" if "/p/" in url else url))
                            if url.startswith(address + "/"):
                                # Only fetch the local hop. Browser redirects bypass routing;
                                # the resolver rule above blocks them before any external DNS.
                                response = await route.fetch(max_redirects=0, timeout=5000)
                                await route.fulfill(response=response)
                            else:
                                await route.abort()

                        await ctx.route("**/*", intercept)
                        page = await ctx.new_page()
                        # The expected end is an allowed navigation attempt stopped by DNS,
                        # never a real external page. A CSP refusal is a different failure.
                        async with page.expect_event("requestfailed", predicate=lambda r: r.url.startswith("https://yoomoney.ru/"), timeout=5000) as blocked:
                            await page.goto(address + "/p/" + label, wait_until="commit", timeout=5000)
                            if not javascript:
                                await page.get_by_role("button", name="Перейти к оплате").click(no_wait_after=True)
                        attempted = await blocked.value
                        assert attempted.failure in ("net::ERR_NAME_NOT_RESOLVED", "net::ERR_FAILED"), traffic
                        assert service.calls == 1
                        assert yk_api.bodies[0]["payment_method_data"] == {"type": "sbp"}
                        assert await count_rows(db, "payments") == 1
                    finally:
                        await browser.close()
                async with ClientSession() as http:
                    async with http.get(address + "/inhouse/pay") as response:
                        assert response.status == 405
                    async with http.post(address + "/inhouse/pay", data={"t": "x" * 3000}) as response:
                        assert response.status == 413
                    await upstream.close()
                    async with http.post(address + "/inhouse/pay", data={"t": "test"}) as response:
                        assert response.status == 502
                        body = await response.text()
                        assert "Оплата временно недоступна" in body and deps.bot_link in body
            finally:
                proc.terminate()
                proc.wait(timeout=5)
