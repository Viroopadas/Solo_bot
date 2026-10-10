import os

import pytest

from modules.inhouse.features.yookassa_methods import token
from modules.inhouse.tests.helpers import SECRET


NOW = 1_760_000_000_000


def test_sign_verify_roundtrip():
    t = token.issue(SECRET, 123456789, "sbp", 1800, issued_ms=NOW)
    claims = token.verify(SECRET, t, 1800, now=NOW + 1000)
    assert claims == token.Claims(tg_id=123456789, method="sbp", issued_ms=NOW, ttl_s=1800)
    assert len(t.split(".")) == 6


@pytest.mark.parametrize("method", ["sbp", "card", "other"])
def test_all_methods(method):
    t = token.issue(SECRET, 42, method, 1800, issued_ms=NOW)
    assert token.verify(SECRET, t, 1800, now=NOW).method == method


def test_expiry():
    t = token.issue(SECRET, 42, "card", 1800, issued_ms=NOW)
    token.verify(SECRET, t, 1800, now=NOW + 1800 * 1000)  # last millisecond still valid
    with pytest.raises(token.TokenExpiredError):
        token.verify(SECRET, t, 1800, now=NOW + 1800 * 1000 + 1)


def test_server_ttl_caps_label_ttl():
    t = token.issue(SECRET, 42, "card", 86400, issued_ms=NOW)
    with pytest.raises(token.TokenExpiredError):
        token.verify(SECRET, t, 1800, now=NOW + 1801 * 1000)


def test_issued_in_future_rejected():
    t = token.issue(SECRET, 42, "card", 1800, issued_ms=NOW + 10 * 60 * 1000)
    with pytest.raises(token.TokenError):
        token.verify(SECRET, t, 1800, now=NOW)


def _swap(t: str, index: int, value: str) -> str:
    parts = t.split(".")
    parts[index] = value
    return ".".join(parts)


@pytest.mark.parametrize(
    "index,value",
    [(1, "987654321"), (2, "c"), (3, str(NOW + 5000)), (4, "99999"), (0, "2")],
)
def test_tampered_fields_rejected(index, value):
    t = token.issue(SECRET, 123456789, "sbp", 1800, issued_ms=NOW)
    with pytest.raises(token.TokenError) as err:
        token.verify(SECRET, _swap(t, index, value), 1800, now=NOW)
    assert not isinstance(err.value, token.TokenExpiredError)


def test_tampered_signature_and_wrong_secret():
    t = token.issue(SECRET, 42, "sbp", 1800, issued_ms=NOW)
    sig = t.rsplit(".", 1)[1]
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    with pytest.raises(token.TokenError):
        token.verify(SECRET, t.rsplit(".", 1)[0] + "." + flipped, 1800, now=NOW)
    with pytest.raises(token.TokenError):
        token.verify(bytes(32), t, 1800, now=NOW)


@pytest.mark.parametrize(
    "bad", ["", "abc", "1.2.3", "x" * 500, "1.42.s.1.1.ЖЖЖ", "1.42.z.1760000000000.1800.sig", None]
)
def test_garbage_rejected(bad):
    with pytest.raises(token.TokenError):
        token.verify(SECRET, bad, 1800, now=NOW)


def test_unknown_method_cannot_be_issued():
    with pytest.raises(ValueError):
        token.issue(SECRET, 42, "crypto", 1800)


def test_secret_created_once_and_reused(tmp_path):
    path = tmp_path / "sub" / "yk.secret"
    first = token.load_or_create_secret(path)
    assert len(first) == 32
    assert token.load_or_create_secret(path) == first
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_short_secret_file_refused(tmp_path):
    path = tmp_path / "yk.secret"
    path.write_text("abcd", encoding="ascii")
    with pytest.raises(ValueError):
        token.load_or_create_secret(path)
