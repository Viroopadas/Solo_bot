"""Payment method label for the current request.

The label lives in a ContextVar, so every request has its own and two buyers never mix.
asyncio.to_thread copies the context, so the label also reaches the thread in which the
stock YooKassa service calls the SDK. The holder ("pocket") is a new mutable object per
call: the SDK wrapper writes the payment id and URL into it, and the endpoint reads them
after the stock function returns.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


METHODS: tuple[str, ...] = ("sbp", "card", "other")


@dataclass
class Holder:
    payment_id: str | None = None
    confirmation_url: str | None = None
    applied: bool = False  # payment_method_data was added to the request
    conflict: bool = False  # the request already had a method field; nothing was changed
    calls: int = 0  # Payment.create calls seen in this scope
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Selection:
    method: str
    holder: Holder


_current: ContextVar[Selection | None] = ContextVar("inhouse_yk_method", default=None)


@contextmanager
def method_scope(method: str) -> Iterator[Holder]:
    """Set the method label for the duration of the block; always removed in finally."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")
    holder = Holder()
    token = _current.set(Selection(method=method, holder=holder))
    try:
        yield holder
    finally:
        _current.reset(token)


def current() -> Selection | None:
    return _current.get()
