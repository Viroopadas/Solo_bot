"""POST /inhouse/pay through a real aiohttp server: commit/rollback, pages, headers."""

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yookassa import Payment

from modules.inhouse.features.yookassa_methods import pay_endpoint, sdk_patch, state, token
from modules.inhouse.tests.helpers import (
    SECRET,
    FakeStockService,
    activate,
    count_rows,
    make_deps,
    past,
    save_checkout,
)


TG = 888000333


@pytest.fixture
async def client():
    app = web.Application()
    app.router.add_post("/inhouse/pay", pay_endpoint.handle)  # as web/__init__.py does
    async with TestClient(TestServer(app)) as c:
        yield c


async def post(client, t, path_form=False, **headers):
    value = f"/p/{t}" if path_form else t
    return await client.post("/inhouse/pay", data={"t": value}, headers=headers, allow_redirects=False)


def label(method="sbp"):
    return token.issue(SECRET, TG, method, 1800)


def assert_safe_headers(resp):
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "DENY"


async def test_success_commits_and_redirects(client, db, yk_api, logs):
    svc = FakeStockService()
    activate(make_deps(db, svc))
    sdk_patch.install(Payment)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label("sbp")
    resp = await post(client, t, path_form=True, Origin="null")
    assert resp.status == 303, logs.text()
    assert resp.headers["Location"].startswith("https://yoomoney.ru/checkout/")
    assert_safe_headers(resp)
    assert yk_api.bodies[0]["payment_method_data"] == {"type": "sbp"}
    assert await count_rows(db, "payments") == 1  # stock row committed by OUR session
    assert await count_rows(db, "inhouse_yk_links") == 1
    text = logs.text()
    assert "result=created method=sbp" in text
    assert t not in text and "yoomoney.ru" not in text  # no labels or payment URLs in the log


async def test_stock_error_rolls_back(client, db, yk_api):
    svc = FakeStockService(fail=RuntimeError("kassa down"))
    activate(make_deps(db, svc))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post(client, label())
    assert resp.status == 502
    body = await resp.text()
    assert "Не получилось создать оплату" in body and "https://t.me/example_bot" in body
    assert 'name="t"' in body  # retry form
    assert await count_rows(db, "payments") == 0 and await count_rows(db, "inhouse_yk_links") == 0


async def test_store_write_failure_rolls_back_payment_row(client, db, yk_api, monkeypatch):
    svc = FakeStockService()
    activate(make_deps(db, svc))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))

    async def broken_insert(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pay_endpoint.store, "insert", broken_insert)
    resp = await post(client, label())
    assert resp.status == 502
    assert await count_rows(db, "payments") == 0  # the stock row did not survive


async def test_none_from_stock_is_error(client, db, yk_api):
    """None without an SDK call (provider refused) -> error page, rollback."""

    async def refuse(session, tg_id, amount):
        return None

    activate(make_deps(db, refuse))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post(client, label())
    assert resp.status == 502 and await count_rows(db, "inhouse_yk_links") == 0


async def test_none_after_sdk_call_is_error(client, db, yk_api):
    """Stock function created the YooKassa payment but returned None -> error page, rollback."""
    activate(make_deps(db, FakeStockService(return_none=True)))
    sdk_patch.install(Payment)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post(client, label())
    assert resp.status == 502
    assert await count_rows(db, "payments") == 0 and await count_rows(db, "inhouse_yk_links") == 0


async def test_dict_fallback_when_sdk_not_wrapped(client, db, yk_api):
    """Wrapper not seeing the call: id/url come from the stock function's dict."""
    svc = FakeStockService()
    activate(make_deps(db, svc))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post(client, label("card"))
    assert resp.status == 303 and resp.headers["Location"].startswith("https://yoomoney.ru/")


async def test_foreign_redirect_refused(client, db):
    async def evil(session, tg_id, amount):
        return {"id": "p1", "confirmation_url": "https://evil.example/pay"}

    activate(make_deps(db, evil))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post(client, label())
    assert resp.status == 502 and await count_rows(db, "inhouse_yk_links") == 0


async def test_unavailable_when_feature_off(client, db):
    resp = await post(client, label())
    assert resp.status == 503
    assert "Оплата временно недоступна" in await resp.text()
    assert_safe_headers(resp)


async def test_unavailable_when_module_off(client, db, monkeypatch):
    activate(make_deps(db, FakeStockService()))
    monkeypatch.setattr(state, "module_enabled", lambda: False)
    resp = await post(client, label())
    assert resp.status == 503


async def test_broken_label_page(client, db):
    activate(make_deps(db, FakeStockService()))
    resp = await post(client, "1.2.s.3.4.forged")
    assert resp.status == 400
    body = await resp.text()
    assert "Ссылка недействительна" in body and "Traceback" not in body


async def test_forged_label_never_opens_database_even_for_purge(client):
    """Spec 4.2/4.3: signature checks precede ALL database access."""
    calls = []

    def forbidden_database():
        calls.append("opened")
        raise AssertionError("unsigned request reached database")

    activate(make_deps(forbidden_database, FakeStockService()))
    resp = await post(client, "1.2.s.3.4.forged")
    assert resp.status == 400
    assert calls == []


async def test_expired_label_never_opens_database(client):
    """Spec 4.2 step 3: the expiry check, like HMAC, precedes database access."""
    calls = []

    def forbidden_database():
        calls.append("opened")
        raise AssertionError("expired request reached database")

    activate(make_deps(forbidden_database, FakeStockService()))
    expired = token.issue(SECRET, TG, "sbp", 1800, issued_ms=token.now_ms() - 1801000)
    resp = await post(client, expired)
    assert resp.status == 410
    assert calls == []


async def test_stale_page(client, db):
    activate(make_deps(db, FakeStockService()))
    resp = await post(client, label())
    assert resp.status == 410 and "Оформление устарело" in await resp.text()


async def test_paid_page(client, db, yk_api):
    from modules.inhouse.tests.helpers import set_payment_status

    activate(make_deps(db, FakeStockService()))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label()
    first = await post(client, t)
    async with db() as session:
        rows = await pay_endpoint.store.rows_for_link(session, TG, token.verify(SECRET, t, 1800).issued_ms)
    pid = rows[0].payment_id
    await set_payment_status(db, pid, "success")
    resp = await post(client, t)
    assert first.status == 303 and resp.status == 409 and "уже оплачена" in await resp.text()


async def test_foreign_origin_refused(client, db):
    activate(make_deps(db, FakeStockService()))
    resp = await post(client, label(), Origin="https://evil.example")
    assert resp.status == 400


async def test_allowed_origin_accepted(client, db, yk_api):
    activate(make_deps(db, FakeStockService()))
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    resp = await post(client, label(), Origin="https://pay.omenso.net")
    assert resp.status == 303


async def test_oversized_body_refused(client, db):
    activate(make_deps(db, FakeStockService()))
    resp = await client.post("/inhouse/pay", data={"t": "x" * 5000}, allow_redirects=False)
    assert resp.status == 400


async def test_get_not_served(client):
    resp = await client.get("/inhouse/pay", allow_redirects=False)
    assert resp.status == 405
