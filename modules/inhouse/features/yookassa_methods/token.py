"""Signed label inside a link button.

Format: "1.<tg_id>.<m>.<issued_ms>.<ttl_s>.<sig>", where m is s (SBP), c (card) or
o (other), issued_ms is the issue time in Unix milliseconds and sig is base64url of
HMAC-SHA256(secret, everything before the last dot). The label does not hide the
person's id, but it cannot be forged. No amount inside: the amount is taken on the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from dataclasses import dataclass
from pathlib import Path


VERSION = "1"
METHOD_CODES: dict[str, str] = {"sbp": "s", "card": "c", "other": "o"}
CODE_METHODS: dict[str, str] = {v: k for k, v in METHOD_CODES.items()}
SECRET_BYTES = 32
MAX_TOKEN_LEN = 200
FUTURE_SKEW_MS = 60_000


class TokenError(Exception):
    """The label is broken, forged or of an unknown version."""


class TokenExpiredError(TokenError):
    """The label is genuine but its time is over; `claims` holds its (verified) fields."""

    def __init__(self, message: str, claims: Claims | None = None) -> None:
        super().__init__(message)
        self.claims = claims


@dataclass(frozen=True)
class Claims:
    tg_id: int
    method: str
    issued_ms: int
    ttl_s: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign(secret: bytes, payload: str) -> str:
    return _b64(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def issue(secret: bytes, tg_id: int, method: str, ttl_s: int, issued_ms: int | None = None) -> str:
    if method not in METHOD_CODES:
        raise ValueError(f"unknown method {method!r}")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool) or tg_id <= 0:
        raise ValueError("tg_id must be a positive int")
    if len(secret) < SECRET_BYTES:
        raise ValueError("secret too short")
    issued = now_ms() if issued_ms is None else int(issued_ms)
    payload = f"{VERSION}.{tg_id}.{METHOD_CODES[method]}.{issued}.{int(ttl_s)}"
    return f"{payload}.{_sign(secret, payload)}"


def verify(secret: bytes, token: str, max_ttl_s: int, now: int | None = None) -> Claims:
    """Check signature first, then fields and time. Raises TokenError / TokenExpiredError."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
        raise TokenError("bad length")
    payload, sep, sig = token.rpartition(".")
    if not sep or not payload or not sig:
        raise TokenError("bad shape")
    try:
        expected = _sign(secret, payload)
    except UnicodeEncodeError as exc:
        raise TokenError("bad charset") from exc
    if not hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii", "replace")):
        raise TokenError("bad signature")
    parts = payload.split(".")
    if len(parts) != 5:
        raise TokenError("bad shape")
    version, tg_raw, code, issued_raw, ttl_raw = parts
    if version != VERSION:
        raise TokenError("unknown version")
    if code not in CODE_METHODS:
        raise TokenError("unknown method")
    if not (tg_raw.isdigit() and issued_raw.isdigit() and ttl_raw.isdigit()):
        raise TokenError("bad number")
    tg_id, issued_ms, ttl_s = int(tg_raw), int(issued_raw), int(ttl_raw)
    if tg_id <= 0:
        raise TokenError("bad tg_id")
    current = now_ms() if now is None else int(now)
    if issued_ms > current + FUTURE_SKEW_MS:
        raise TokenError("issued in the future")
    claims = Claims(tg_id=tg_id, method=CODE_METHODS[code], issued_ms=issued_ms, ttl_s=ttl_s)
    if current > issued_ms + min(ttl_s, int(max_ttl_s)) * 1000:
        raise TokenExpiredError("expired", claims)
    return claims


def load_or_create_secret(path: Path) -> bytes:
    """Read the feature secret; create it (256 bit, mode 0600) on first start. Never logged."""
    path = Path(path)
    if path.exists():
        raw = path.read_text(encoding="ascii").strip()
        try:
            secret = bytes.fromhex(raw)
        except ValueError as exc:
            raise ValueError("secret file is not hex") from exc
        if len(secret) < SECRET_BYTES:
            raise ValueError("secret file too short")
        return secret
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(SECRET_BYTES)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secret.hex().encode("ascii"))
    finally:
        os.close(fd)
    return secret
