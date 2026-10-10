"""Expiry dates of certificates and domains for the watch (spec section 3.7).

- certificate: a fresh TLS connection per name with normal chain and name verification;
- .net/.com domain: public RDAP of Verisign; .ru/.su/.рф domain: whois.tcinet.ru, paid-till;
  other zones: rdap.org (public, no registration).
Every function returns days left (float, may be negative) or raises; the watch counts a
raise as "could not check". Parsers are separate so tests feed them forged answers.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time

from datetime import datetime, timezone


WHOIS_RU = "whois.tcinet.ru"
WHOIS_RU_ZONES = ("ru", "su", "xn--p1ai")
VERISIGN_ZONES = ("net", "com")
READ_LIMIT = 65536
X509_V_ERR_CERT_HAS_EXPIRED = 10

# Seams for tests: replaced by fakes, never by real network in tests.
open_connection = asyncio.open_connection
now = time.time


class ExpiryError(Exception):
    """The date could not be learned; the message is safe to log."""


def _parse_iso(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def cert_not_after(cert: dict | None) -> float:
    if not cert or not cert.get("notAfter"):
        raise ExpiryError("no certificate date")
    return float(ssl.cert_time_to_seconds(cert["notAfter"]))


def parse_rdap_expiry(raw: bytes | str) -> float:
    try:
        data = json.loads(raw)
    except ValueError:
        raise ExpiryError("rdap answer is not json") from None
    for event in data.get("events") or []:
        if isinstance(event, dict) and event.get("eventAction") == "expiration" and event.get("eventDate"):
            try:
                return _parse_iso(str(event["eventDate"]))
            except ValueError:
                raise ExpiryError("rdap expiration date unreadable") from None
    raise ExpiryError("rdap answer has no expiration")


_PAID_TILL = re.compile(r"^\s*paid-till:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def parse_whois_paid_till(text: str) -> float:
    match = _PAID_TILL.search(text)
    if not match:
        raise ExpiryError("whois answer has no paid-till")
    try:
        return _parse_iso(match.group(1))
    except ValueError:
        raise ExpiryError("whois paid-till unreadable") from None


def _days(epoch: float) -> float:
    return (epoch - now()) / 86400.0


async def cert_days(host: str, timeout: float = 10.0) -> float:
    ctx = ssl.create_default_context()
    try:
        _reader, writer = await asyncio.wait_for(
            open_connection(host, 443, ssl=ctx, server_hostname=host), timeout
        )
    except ssl.SSLCertVerificationError as exc:
        if getattr(exc, "verify_code", None) == X509_V_ERR_CERT_HAS_EXPIRED:
            return -1.0
        raise ExpiryError(f"certificate not verified ({getattr(exc, 'verify_code', '?')})") from None
    try:
        cert = writer.get_extra_info("peercert")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception:  # closing errors do not matter here
            pass
    return _days(cert_not_after(cert))


async def whois_text(server: str, query: str, timeout: float = 10.0) -> str:
    reader, writer = await asyncio.wait_for(open_connection(server, 43), timeout)
    try:
        writer.write(query.encode("idna") + b"\r\n")
        await writer.drain()
        chunks: list[bytes] = []
        size = 0
        while size < READ_LIMIT:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception:
            pass
    return b"".join(chunks).decode("utf-8", "replace")


async def rdap_json(url: str, timeout: float = 10.0) -> bytes:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Accept": "application/rdap+json, application/json"},
        ) as resp:
            if resp.status != 200:
                raise ExpiryError(f"rdap status {resp.status}")
            return await resp.content.read(READ_LIMIT)


def rdap_url(domain: str) -> str:
    zone = domain.rsplit(".", 1)[-1].lower()
    if zone in VERISIGN_ZONES:
        return f"https://rdap.verisign.com/{zone}/v1/domain/{domain}"
    return f"https://rdap.org/domain/{domain}"


async def domain_days(domain: str, timeout: float = 10.0) -> float:
    zone = domain.encode("idna").decode("ascii").rsplit(".", 1)[-1].lower()
    if zone in WHOIS_RU_ZONES:
        return _days(parse_whois_paid_till(await whois_text(WHOIS_RU, domain, timeout)))
    return _days(parse_rdap_expiry(await rdap_json(rdap_url(domain), timeout)))
