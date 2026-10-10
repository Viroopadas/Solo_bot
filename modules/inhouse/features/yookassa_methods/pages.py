"""Small HTML pages the endpoint answers with instead of a technical error."""

from __future__ import annotations

import html

from types import ModuleType

from aiohttp import web


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


def page_csp(settings: ModuleType) -> str:
    """CSP of our pages. form-action must also allow YooKassa: browsers apply it to the
    redirect that follows the retry form post."""
    hosts = getattr(settings, "YOOKASSA_REDIRECT_HOSTS", ())
    allowed = " ".join(f"https://{h} https://*.{h}" for h in hosts)
    return (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action 'self' {allowed}; frame-ancestors 'none'; base-uri 'none'"
    )


# kind -> (settings attribute with the text, HTTP status)
KINDS: dict[str, tuple[str, int]] = {
    "stale": ("PAGE_STALE", 410),
    "paid": ("PAGE_PAID", 409),
    "error": ("PAGE_ERROR", 502),
    "unavailable": ("PAGE_UNAVAILABLE", 503),
    "invalid": ("PAGE_INVALID", 400),
    "limited": ("PAGE_LIMITED", 429),
}

_STYLE = (
    "body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f5f7;color:#1c1c1e;"
    "margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}"
    "main{background:#fff;border-radius:16px;padding:28px 24px;max-width:420px;margin:16px;"
    "box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}"
    "p{font-size:18px;line-height:1.4;margin:0 0 20px}"
    "a,button{display:block;width:100%;box-sizing:border-box;padding:14px;border-radius:12px;font-size:17px;"
    "text-decoration:none;border:0;margin-top:10px;cursor:pointer}"
    "a{background:#2481cc;color:#fff}button{background:#e8eef5;color:#1c1c1e}"
)


def render(kind: str, settings: ModuleType, bot_link: str = "", retry_token: str = "") -> web.Response:
    attr, status = KINDS[kind]
    message = getattr(settings, attr, "") or ""
    title = getattr(settings, "PAGE_TITLE", "Оплата")
    parts = [f"<p>{html.escape(message)}</p>"]
    if kind == "error" and retry_token:
        action = html.escape(getattr(settings, "WEBHOOK_PATH", "/inhouse/pay"), quote=True)
        parts.append(
            f'<form method="post" action="{action}">'
            f'<input type="hidden" name="t" value="{html.escape(retry_token, quote=True)}">'
            f'<button type="submit">{html.escape(getattr(settings, "PAGE_ERROR_RETRY", "Ещё раз"))}</button>'
            "</form>"
        )
    if bot_link:
        link_text = getattr(settings, "BOT_LINK_TEXT", "Вернуться в бот")
        parts.append(f'<a href="{html.escape(bot_link, quote=True)}">{html.escape(link_text)}</a>')
    body = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{''.join(parts)}</main></body></html>"
    )
    response = web.Response(status=status, text=body, content_type="text/html", charset="utf-8")
    apply_headers(response)
    response.headers["Content-Security-Policy"] = page_csp(settings)
    return response


def apply_headers(response: web.StreamResponse) -> None:
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
