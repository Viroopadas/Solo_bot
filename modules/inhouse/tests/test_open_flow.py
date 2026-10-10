"""open_link: stale checks, amount bounds, reuse, already paid, lock, hourly limit."""

import asyncio

from datetime import timedelta

import pytest

from modules.inhouse.features.yookassa_methods import pay_endpoint, store, token
from modules.inhouse.tests.helpers import (
    SECRET,
    FakeStockService,
    clear_checkout,
    count_rows,
    make_deps,
    past,
    save_checkout,
    set_payment_status,
)


TG = 555000111


def label(method="sbp", issued_ms=None):
    return token.issue(SECRET, TG, method, 1800, issued_ms=issued_ms)


async def test_created_then_reused_same_payment(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label("sbp")
    first = await pay_endpoint.open_link(deps, t)
    assert first.kind == "created" and first.url.startswith("https://yoomoney.ru/")
    again = await pay_endpoint.open_link(deps, t)
    assert again.kind == "reused" and again.url == first.url and again.payment_id == first.payment_id
    assert svc.calls == 1
    assert await count_rows(db, "inhouse_yk_links") == 1
    # amount came from the saved purchase, not from the link
    assert yk_api.bodies[0]["amount"] == {"value": "199.00", "currency": "RUB"}


async def test_new_screen_same_checkout_reuses(db, yk_api):
    """A re-sent screen (new label) for the same checkout opens the same unpaid payment."""
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(5))
    a = await pay_endpoint.open_link(deps, label("card", token.now_ms() - 60_000))
    b = await pay_endpoint.open_link(deps, label("card"))
    assert (a.kind, b.kind) == ("created", "reused") and a.payment_id == b.payment_id


async def test_methods_get_separate_payments(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    sbp = await pay_endpoint.open_link(deps, label("sbp"))
    card = await pay_endpoint.open_link(deps, label("card"))
    other = await pay_endpoint.open_link(deps, label("other"))
    assert len({sbp.payment_id, card.payment_id, other.payment_id}) == 3
    assert svc.calls == 3


async def test_no_checkout_is_stale(db, yk_api):
    svc = FakeStockService()
    out = await pay_endpoint.open_link(make_deps(db, svc), label())
    assert out.kind == "stale" and out.reason == "no checkout" and svc.calls == 0


async def test_checkout_newer_than_label_is_stale(db, yk_api):
    """Old button after a new purchase of the same tariff: same amount, newer save time."""
    svc = FakeStockService()
    deps = make_deps(db, svc)
    issued = token.now_ms() - 120_000
    await save_checkout(db, TG, {"required_amount": 199}, past(1))  # saved after the label
    out = await pay_endpoint.open_link(deps, label("sbp", issued))
    assert out.kind == "stale" and out.reason == "checkout newer than label" and svc.calls == 0


async def test_checkout_saved_same_millisecond_is_fresh(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    saved = await save_checkout(db, TG, {"required_amount": 199}, past(0.5))
    from modules.inhouse.features.yookassa_methods.compat import to_ms

    out = await pay_endpoint.open_link(deps, label("sbp", to_ms(saved)))
    assert out.kind == "created"


@pytest.mark.parametrize("amount", [0, -5, 50_001, 199.5, "199", None, True])
async def test_amount_bounds(db, yk_api, amount):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": amount}, past(1))
    out = await pay_endpoint.open_link(deps, label())
    assert out.kind == "stale" and out.reason == "amount out of bounds" and svc.calls == 0


async def test_amount_upper_bound_inclusive(db, yk_api):
    deps = make_deps(db, FakeStockService())
    await save_checkout(db, TG, {"required_amount": 50_000}, past(1))
    assert (await pay_endpoint.open_link(deps, label())).kind == "created"


async def test_expired_and_broken_labels(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(40))
    expired = await pay_endpoint.open_link(deps, label("sbp", token.now_ms() - 31 * 60 * 1000))
    broken = await pay_endpoint.open_link(deps, label("sbp")[:-3] + "AAA")
    assert expired.kind == "stale" and broken.kind == "invalid" and svc.calls == 0


async def test_paid_payment_shows_paid(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label("sbp")
    first = await pay_endpoint.open_link(deps, t)
    await set_payment_status(db, first.payment_id, "success")
    out = await pay_endpoint.open_link(deps, t)
    assert out.kind == "paid" and svc.calls == 1


async def test_paid_with_other_method_shows_paid(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    first = await pay_endpoint.open_link(deps, label("sbp"))
    await set_payment_status(db, first.payment_id, "success")
    out = await pay_endpoint.open_link(deps, label("card"))
    assert out.kind == "paid" and svc.calls == 1


async def test_paid_after_checkout_cleared_shows_paid(db, yk_api):
    """After payment the bot clears temporary_data; the paid link still says 'already paid'."""
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label("card")
    first = await pay_endpoint.open_link(deps, t)
    await set_payment_status(db, first.payment_id, "success")
    await clear_checkout(db, TG)
    out = await pay_endpoint.open_link(deps, t)
    assert out.kind == "paid"


async def test_expired_label_of_paid_payment_is_stale(db, yk_api, monkeypatch):
    """Spec 4.2 step 3: expiry must be rejected before even a paid lookup."""
    svc = FakeStockService()
    deps = make_deps(db, svc)
    issued = token.now_ms() - 29 * 60 * 1000
    await save_checkout(db, TG, {"required_amount": 199}, past(30))
    t = label("sbp", issued)
    first = await pay_endpoint.open_link(deps, t)
    assert first.kind == "created"
    await set_payment_status(db, first.payment_id, "success")
    real = token.now_ms
    monkeypatch.setattr(token, "now_ms", lambda: real() + 2 * 60 * 1000)  # two minutes later: expired
    out = await pay_endpoint.open_link(deps, t)
    assert out.kind == "stale"
    await set_payment_status(db, first.payment_id, "pending")
    assert (await pay_endpoint.open_link(deps, t)).kind == "stale"


@pytest.mark.parametrize("status", ["canceled", "cancelled", "failed"])
async def test_cancelled_payment_gets_new_one(db, yk_api, status):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label("sbp")
    first = await pay_endpoint.open_link(deps, t)
    await set_payment_status(db, first.payment_id, status)
    second = await pay_endpoint.open_link(deps, t)
    assert second.kind == "created" and second.payment_id != first.payment_id and svc.calls == 2


async def test_two_concurrent_opens_one_payment(db, yk_api):
    svc = FakeStockService(delay=0.2)
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label("sbp")
    a, b = await asyncio.gather(pay_endpoint.open_link(deps, t), pay_endpoint.open_link(deps, t))
    assert sorted([a.kind, b.kind]) == ["created", "reused"]
    assert a.url == b.url and svc.calls == 1
    assert await count_rows(db, "payments") == 1
    assert len(store.LOCKS) == 0  # lock entries do not leak


async def test_concurrent_different_methods_complete(db, yk_api):
    """Spec 4.3 limits creation per person; parallel methods may wait for that quota."""
    svc = FakeStockService(delay=0.2)
    deps = make_deps(db, svc)
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    a, b = await asyncio.wait_for(
        asyncio.gather(pay_endpoint.open_link(deps, label("sbp")), pay_endpoint.open_link(deps, label("card"))),
        timeout=3,
    )
    assert a.kind == b.kind == "created" and a.payment_id != b.payment_id
    assert len(store.LOCKS) == 0


async def test_concurrent_methods_cannot_exceed_hourly_limit(db, yk_api):
    """Spec 4.3/4.6: at most ten new invoices per person per hour."""
    svc = FakeStockService(delay=0.1)
    deps = make_deps(db, svc)
    async with db() as session:
        for i in range(9):
            await store.insert(session, tg_id=TG, checkout_ms=i, issued_ms=i,
                               method="sbp", payment_id=f"old-{i}",
                               confirmation_url="https://yoomoney.ru/x", amount=1)
        await session.commit()
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    results = await asyncio.wait_for(asyncio.gather(
        pay_endpoint.open_link(deps, label("sbp")),
        pay_endpoint.open_link(deps, label("card")),
    ), timeout=3)
    assert sorted(r.kind for r in results) == ["created", "limited"]
    assert svc.calls == 1
    assert await count_rows(db, "inhouse_yk_links") == 10
    assert len(store.LOCKS) == 0


async def test_hourly_limit(db, yk_api):
    svc = FakeStockService()
    deps = make_deps(db, svc)
    async with db() as session:
        for i in range(10):
            await store.insert(
                session,
                tg_id=TG,
                checkout_ms=i,
                issued_ms=i,
                method="sbp",
                payment_id=f"old-{i}",
                confirmation_url="https://yoomoney.ru/x",
                amount=1,
            )
        await session.commit()
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    out = await pay_endpoint.open_link(deps, label())
    assert out.kind == "limited" and svc.calls == 0


@pytest.mark.parametrize("lock_kind", ["checkout", "person"])
async def test_checkout_changed_while_waiting_never_creates_payment(db, yk_api, lock_kind):
    """T5: a link to the previous purchase must not create an invoice."""
    from modules.inhouse.features.yookassa_methods import compat

    svc = FakeStockService()
    deps = make_deps(db, svc)
    saved = await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label()
    read_done = asyncio.Event()

    async def observed_read(session, tg_id):
        checkout = await compat.read_checkout(session, tg_id)
        read_done.set()
        return checkout

    deps.read_checkout = observed_read
    key = (TG, compat.to_ms(saved), "sbp") if lock_kind == "checkout" else ("new-payment", TG)
    async with store.LOCKS.hold(key):
        task = asyncio.create_task(pay_endpoint.open_link(deps, t))
        try:
            await asyncio.wait_for(read_done.wait(), timeout=2)
            await save_checkout(db, TG, {"required_amount": 299}, past(0))
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    out = await asyncio.wait_for(task, timeout=3)
    assert out.kind == "stale" and svc.calls == 0
    assert await count_rows(db, "payments") == 0
    assert len(store.LOCKS) == 0


async def test_reuse_survives_process_memory_reset(db, yk_api):
    """T4: persisted rows, not process memory, decide reuse after restart."""
    svc = FakeStockService()
    await save_checkout(db, TG, {"required_amount": 199}, past(1))
    t = label()
    first = await pay_endpoint.open_link(make_deps(db, svc), t)
    store.LOCKS._locks.clear()
    restarted_service = FakeStockService()
    again = await pay_endpoint.open_link(make_deps(db, restarted_service), t)
    assert again.kind == "reused" and again.payment_id == first.payment_id
    assert restarted_service.calls == 0


async def test_old_rows_purged(db):
    import time

    async with db() as session:
        await store.insert(
            session,
            tg_id=TG,
            checkout_ms=1,
            issued_ms=1,
            method="sbp",
            payment_id="a",
            confirmation_url="https://yoomoney.ru/x",
            amount=1,
            now_s=int(time.time()) - 8 * 86400,
        )
        await store.insert(
            session,
            tg_id=TG,
            checkout_ms=2,
            issued_ms=2,
            method="sbp",
            payment_id="b",
            confirmation_url="https://yoomoney.ru/x",
            amount=1,
        )
        assert await store.purge_old(session, 7) == 1
        await session.commit()
    assert await count_rows(db, "inhouse_yk_links") == 1


def test_checkout_ms_is_utc():
    from datetime import datetime, timezone

    from modules.inhouse.features.yookassa_methods.compat import to_ms

    naive = datetime(2026, 10, 10, 12, 0, 0, 500000)
    assert to_ms(naive) == int(naive.replace(tzinfo=timezone.utc).timestamp() * 1000)
    assert to_ms(naive + timedelta(milliseconds=1)) == to_ms(naive) + 1
