from __future__ import annotations

from html import escape
from typing import Any


def _clip(value: Any, limit: int = 80) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def format_poll_list(polls: list[dict]) -> str:
    lines = ["<b>Последние опросы</b>"]
    for poll in polls:
        lines.append(
            f"#{poll.get('id')} | {escape(str(poll.get('status') or 'unknown'))} | "
            f"{escape(_clip(poll.get('question'), 70))}"
        )
    return "\n".join(lines)


def format_poll_created(poll: dict) -> str:
    return (
        "Опрос создан.\n"
        f"Внутренний ID: <code>{poll.get('id')}</code>\n"
        f"Telegram poll_id: <code>{escape(str(poll.get('poll_id') or ''))}</code>"
    )


def format_poll_stats(stats: dict[str, Any]) -> str:
    poll = stats["poll"]
    options = stats["options"]
    counts = stats["counts"]

    lines = [
        f"<b>Опрос #{poll.get('id')}</b>",
        f"Статус: {escape(str(poll.get('status') or 'unknown'))}",
        f"Poll ID: <code>{escape(str(poll.get('poll_id') or ''))}</code>",
        f"Вопрос: {escape(str(poll.get('question') or ''))}",
        "",
        f"Ответили: {stats['respondents']}",
        f"Сняли ответ: {stats['withdrawn']}",
        "",
        "<b>Варианты</b>",
    ]

    for idx, option in enumerate(options):
        lines.append(f"{idx + 1}. {escape(str(option))}: {counts.get(idx, 0)}")

    if stats.get("allows_multiple_answers"):
        lines.extend(
            [
                "",
                "В этом опросе можно выбрать несколько вариантов, поэтому сумма голосов может быть больше числа людей.",
            ]
        )

    return "\n".join(lines)


POLL_TEST_USAGE = (
    "Использование:\n"
    "<code>/poll_test</code>\n"
    "<code>/poll_test Вопрос | Вариант 1 | Вариант 2</code>\n"
    "<code>/poll_test multi | Вопрос | Вариант 1 | Вариант 2</code>"
)

POLL_STATS_USAGE = "Использование: <code>/poll_stats &lt;id или poll_id&gt;</code>"
POLL_CLOSE_USAGE = "Использование: <code>/poll_close &lt;id или poll_id&gt;</code>"
