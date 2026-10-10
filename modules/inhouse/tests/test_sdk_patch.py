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

from requests import Session


class _FakeSession(Session):
    def __init__(self):
        super().__init__()
        self.closed = 0
        self.heads = []
        self.auth_headers = []

    def close(self):
        self.closed += 1
        super().close()

    def send(self, request, **kwargs):
        from types import SimpleNamespace
        self.heads.append(request.url)
        self.auth_headers.append(request.headers.get("Authorization"))
        return SimpleNamespace(close=lambda: None)


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


def test_session_isolated_adapters_shared_and_close_is_noop(reuse):
    cls, made = reuse
    a, b = cls().get_session(), cls().get_session()
    assert a is not b and len(made) == 2
    assert a.get_adapter("https://example.invalid") is b.get_adapter("https://example.invalid")
    a.close()  # what the SDK does after every request
    assert made[0].closed == 0


def test_uninstall_really_closes_shared_adapters():
    from unittest.mock import Mock
    cls, made = _fake_client_cls()
    sdk_patch.install_session_reuse(cls)
    session = cls().get_session()
    adapter = session.get_adapter("https://example.invalid")
    adapter.close = Mock(wraps=adapter.close)
    sdk_patch.uninstall_session_reuse()
    adapter.close.assert_called_once_with()
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
        assert s1 is not s2
        assert s1.get_adapter("https://api.yookassa.ru") is s2.get_adapter("https://api.yookassa.ru")
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
    assert made[0].auth_headers == [None]


def test_prewarm_noop_without_install():
    assert sdk_patch.prewarm() is False


def test_slow_prewarm_never_starts_second_thread(reuse, monkeypatch):
    started = []

    class HeldThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            started.append(self.target)

    monkeypatch.setattr(sdk_patch.threading, "Thread", HeldThread)
    monkeypatch.setattr(sdk_patch.time, "monotonic", lambda: 1000)
    assert sdk_patch.prewarm() is True
    monkeypatch.setattr(sdk_patch.time, "monotonic", lambda: 1100)
    try:
        assert sdk_patch.prewarm() is False
        assert len(started) == 1
    finally:
        started[0]()


def test_prewarm_does_not_use_netrc_credentials(reuse, monkeypatch):
    import requests.sessions
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda url: pytest.fail("prewarm read netrc"))
    sdk_patch._prewarm_blocking()


def test_prewarm_thread_finishes_and_cannot_overlap(reuse, monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    threads = []
    original_thread = threading.Thread
    original_send = _FakeSession.send

    class RecordingThread(original_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    def held_send(self, request, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_send(self, request, **kwargs)

    monkeypatch.setattr(sdk_patch.threading, "Thread", RecordingThread)
    monkeypatch.setattr(sdk_patch, "PREWARM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(_FakeSession, "send", held_send)
    try:
        assert sdk_patch.prewarm()
        assert entered.wait(timeout=2)
        assert not sdk_patch.prewarm()
        assert len(threads) == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
    assert not sdk_patch._session["warming"]


def test_prewarm_redirect_never_reads_netrc(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import requests.sessions
    from yookassa import Configuration
    from yookassa.client import ApiClient

    requests_seen = []
    netrc_reads = []

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            requests_seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/must-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Configuration.configure("100500", "test_not_a_real_key")
    original_init = ApiClient.__init__

    def local_client(self):
        original_init(self)
        self.endpoint = f"http://127.0.0.1:{server.server_port}/warm"

    monkeypatch.setattr(ApiClient, "__init__", local_client)
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", lambda url: netrc_reads.append(url) or None)
    sdk_patch.install_session_reuse(ApiClient)
    try:
        sdk_patch._prewarm_blocking()
        assert requests_seen == [("/warm", None)]
        assert netrc_reads == []
    finally:
        sdk_patch.uninstall_session_reuse()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_sdk_workers_do_not_share_response_cookies():
    """Owner's thread-safety check: retain the SDK's per-call cookie isolation."""
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from yookassa import Configuration
    from yookassa.client import ApiClient

    ports = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            ports.append(self.client_address[1])
            body = self.headers.get("Cookie", "").encode()
            self.send_response(200)
            if self.path == "/first":
                self.send_header("Set-Cookie", "private=first-call; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Configuration.configure("100500", "test_not_a_real_key")
    sdk_patch.install_session_reuse(ApiClient)
    address = f"http://127.0.0.1:{server.server_port}"
    first_received, second_finished = threading.Event(), threading.Event()

    def call(path):
        session = ApiClient().get_session()
        try:
            if path == "/second":
                assert first_received.wait(timeout=2)
            result = session.get(address + path, timeout=2).text
            if path == "/first":
                first_received.set()
                assert second_finished.wait(timeout=2)
            return result
        finally:
            session.close()
            if path == "/second":
                second_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(call, "/first")
            second = workers.submit(call, "/second")
            assert second.result(timeout=3) == ""
            assert first.result(timeout=3) == ""
        assert len(ports) == 2 and ports[0] == ports[1]  # real connection reused across workers
    finally:
        sdk_patch.uninstall_session_reuse()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
