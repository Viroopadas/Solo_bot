"""Wrapper over yookassa.Payment.create (real SDK 3.9.0, faked HTTP)."""

import asyncio
import copy
import uuid

import pytest

from yookassa import Payment
from yookassa.domain.request import PaymentRequest

from modules.inhouse.features.yookassa_methods import context, sdk_patch, state
from modules.inhouse.tests.helpers import activate, make_deps, stock_payload


@pytest.fixture
def patched(yk_api):
    activate(make_deps(None, None))
    original = Payment.__dict__["create"]
    sdk_patch.install(Payment)
    yield yk_api
    sdk_patch.uninstall()
    assert Payment.__dict__["create"] is original


def test_classmethod_kept(patched):
    raw = Payment.__dict__["create"]
    assert isinstance(raw, classmethod)
    assert raw.__func__.__name__ == "create"


@pytest.mark.parametrize("method,expected", [("sbp", {"type": "sbp"}), ("card", {"type": "bank_card"})])
def test_dict_request_gets_method(patched, method, expected):
    payload = stock_payload(1, 199)
    before = copy.deepcopy(payload)
    key = uuid.uuid4()
    with context.method_scope(method) as holder:
        response = Payment.create(payload, key)
    body = patched.bodies[0]
    assert body["payment_method_data"] == expected
    assert payload == before  # the caller's request is copied, not changed
    assert patched.idempotency_keys[0] == str(key)  # provider's idempotence key passed through
    assert body["amount"] == {"value": "199.00", "currency": "RUB"} and body["receipt"]["items"]
    assert holder.applied and not holder.conflict
    assert holder.payment_id == response.id
    assert holder.confirmation_url == response.confirmation.confirmation_url


def test_payment_request_object(patched):
    request = PaymentRequest(stock_payload(1, 300))
    with context.method_scope("sbp") as holder:
        Payment.create(request, "idem-1")
    assert patched.bodies[0]["payment_method_data"] == {"type": "sbp"}
    assert request.payment_method_data is None
    assert holder.applied and holder.payment_id


def test_keyword_arguments(patched):
    with context.method_scope("card") as holder:
        Payment.create(params=stock_payload(1, 10), idempotency_key="k")
    assert patched.bodies[0]["payment_method_data"] == {"type": "bank_card"}
    assert patched.idempotency_keys[0] == "k" and holder.applied


def test_other_adds_nothing_but_captures(patched):
    with context.method_scope("other") as holder:
        Payment.create(stock_payload(1, 10), "k")
    assert "payment_method_data" not in patched.bodies[0]
    assert holder.payment_id and holder.confirmation_url and not holder.applied


@pytest.mark.parametrize(
    "extra",
    [
        {"payment_method_data": {"type": "sbp"}},
        {"payment_method_id": "2c0a1111-000f-5000-9000-1b2c3d4e5f60"},
        {"payment_token": "tok"},
    ],
)
def test_conflict_unchanged_with_warning(patched, logs, extra):
    payload = {**stock_payload(1, 10), **extra}
    with context.method_scope("card") as holder:
        Payment.create(payload, "k")
    body = patched.bodies[0]
    for key, value in extra.items():
        if key == "payment_method_data":
            assert body[key] == value  # not replaced by bank_card
        else:
            assert body.get(key) == value
    assert holder.conflict and not holder.applied and holder.payment_id
    assert any("not applied" in m for m in logs.at("warning"))


def test_no_label_no_effect(patched):
    Payment.create(stock_payload(1, 10), "k")
    assert "payment_method_data" not in patched.bodies[0]


def test_module_off_no_effect(patched, monkeypatch):
    monkeypatch.setattr(state, "module_enabled", lambda: False)
    with context.method_scope("sbp") as holder:
        Payment.create(stock_payload(1, 10), "k")
    assert "payment_method_data" not in patched.bodies[0]
    assert holder.calls == 0 and holder.payment_id is None


def test_feature_inactive_no_effect(patched):
    state.STATE.active = False
    with context.method_scope("sbp"):
        Payment.create(stock_payload(1, 10), "k")
    assert "payment_method_data" not in patched.bodies[0]


async def test_label_reaches_thread_and_holder_returns(patched):
    """The closed service calls Payment.create via asyncio.to_thread."""
    with context.method_scope("sbp") as holder:
        response = await asyncio.to_thread(Payment.create, stock_payload(1, 10), uuid.uuid4())
    assert patched.bodies[0]["payment_method_data"] == {"type": "sbp"}
    assert holder.payment_id == response.id


async def test_two_buyers_do_not_mix(patched):
    async def buy(method):
        with context.method_scope(method) as holder:
            await asyncio.sleep(0.01)
            await asyncio.to_thread(Payment.create, stock_payload(1, 10), uuid.uuid4())
            return holder

    sbp, card = await asyncio.gather(buy("sbp"), buy("card"))
    types_sent = sorted(b["payment_method_data"]["type"] for b in patched.bodies)
    assert types_sent == ["bank_card", "sbp"]
    assert sbp.payment_id != card.payment_id


def test_label_removed_after_scope(patched):
    with context.method_scope("sbp"):
        pass
    assert context.current() is None


def test_double_install_refused(patched):
    with pytest.raises(sdk_patch.PatchError):
        sdk_patch.install(Payment)


def test_sdk_failure_is_not_retried(patched, monkeypatch):
    """Spec 4.3: the wrapper itself never retries a failed create."""
    from yookassa.client import ApiClient

    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("test transport timeout")

    monkeypatch.setattr(ApiClient, "request", fail)
    with pytest.raises(TimeoutError), context.method_scope("sbp"):
        Payment.create(stock_payload(1, 10), "k")
    assert calls == [1]
    assert context.current() is None


# ---- shared HTTP session to the YooKassa API ------------------------------------------------

class _FakeSession:
    def __init__(self):
        self.closed = 0
        self.heads = []

    def close(self):
        self.closed += 1

    def head(self, url, **kwargs):
        self.heads.append(url)


def _fake_client_cls():
    made = []

    class FakeClient:
        endpoint = "https://api.example.invalid/v3"
        max_attempts = 3
        timeout = 1800

        class configuration:
            verify = True

        def get_session(self):
            s = _FakeSession()
            made.append(s)
            return s

    return FakeClient, made


@pytest.fixture
def reuse():
    cls, made = _fake_client_cls()
    original = cls.__dict__["get_session"]
    sdk_patch.install_session_reuse(cls)
    yield cls, made
    sdk_patch.uninstall_session_reuse()
    assert cls.__dict__["get_session"] is original


def test_session_shared_and_close_is_noop(reuse):
    cls, made = reuse
    a, b = cls().get_session(), cls().get_session()
    assert a is b and len(made) == 1
    a.close()  # what the SDK does after every request
    assert made[0].closed == 0


def test_uninstall_really_closes_shared_session():
    cls, made = _fake_client_cls()
    sdk_patch.install_session_reuse(cls)
    cls().get_session()
    sdk_patch.uninstall_session_reuse()
    assert made[0].closed == 1
    assert cls().get_session() is not made[0]


def test_session_reuse_twice_refused(reuse):
    cls, _ = reuse
    with pytest.raises(sdk_patch.PatchError):
        sdk_patch.install_session_reuse(cls)


def test_real_sdk_session_keeps_retry_adapter():
    from requests.adapters import HTTPAdapter
    from yookassa import Configuration
    from yookassa.client import ApiClient

    Configuration.configure("100500", "test_not_a_real_key")
    sdk_patch.install_session_reuse(ApiClient)
    try:
        s1, s2 = ApiClient().get_session(), ApiClient().get_session()
        assert s1 is s2
        adapter = s1.get_adapter("https://api.yookassa.ru/v3/payments")
        assert isinstance(adapter, HTTPAdapter) and adapter.max_retries.total == ApiClient().max_attempts
        s1.close()
        assert s1.adapters  # still mounted: close() is a no-op while shared
    finally:
        sdk_patch.uninstall_session_reuse()
    assert ApiClient().get_session() is not s1


def test_prewarm_throttled_and_heads_endpoint(reuse, monkeypatch):
    cls, made = reuse
    started = []

    class SyncThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            started.append(1)
            self.target()

    monkeypatch.setattr(sdk_patch.threading, "Thread", SyncThread)
    assert sdk_patch.prewarm() is True
    assert sdk_patch.prewarm() is False  # within the interval
    assert started == [1]
    assert made[0].heads == [cls.endpoint]


def test_prewarm_noop_without_install():
    assert sdk_patch.prewarm() is False
