import io

from logger import logger


_BG = (13, 17, 23)
_PANEL = (22, 27, 34)
_GRID = (48, 54, 61)
_TEXT = (201, 209, 217)
_MUTED = (139, 148, 158)


def _font(size: int):
    from PIL import ImageFont

    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _fmt_value(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.0f}"


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    import math

    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 2, 2.5, 5, 10):
        candidate = mult * base
        if candidate >= value:
            return candidate
    return 10 * base


def render_stats_chart(x_labels: list[str], panels: list[dict]) -> io.BytesIO | None:
    """Рисует вертикальные бар-чарты (по панели на метрику) и возвращает PNG в BytesIO.

    panels: [{"name": str, "color": (r,g,b), "values": [float, ...]}]
    Текст на картинке только ASCII (числа/дни) — кириллица идёт в подписи Telegram.
    """
    try:
        from PIL import Image, ImageDraw

        n = max(1, len(x_labels))
        slot = 44 if n <= 16 else (32 if n <= 24 else 26)
        left_pad, right_pad, top_pad = 58, 22, 16
        plot_h = 150
        title_h = 24
        panel_gap = 18
        axis_h = 28

        plot_w = n * slot
        width = left_pad + right_pad + plot_w
        panel_h = title_h + plot_h
        height = top_pad + len(panels) * (panel_h + panel_gap) + axis_h

        img = Image.new("RGB", (width, height), _BG)
        draw = ImageDraw.Draw(img)
        f_small = _font(15)
        f_title = _font(17)

        show_values = n <= 16
        if n <= 16:
            label_step = 1
        elif n <= 24:
            label_step = 2
        else:
            label_step = 3

        bar_w = int(slot * 0.6)
        bar_off = (slot - bar_w) // 2

        last_baseline = top_pad
        for p_idx, panel in enumerate(panels):
            values = panel.get("values") or [0] * n
            color = panel.get("color", (88, 166, 255))
            name = str(panel.get("name", ""))
            panel_top = top_pad + p_idx * (panel_h + panel_gap)
            plot_top = panel_top + title_h
            baseline = plot_top + plot_h
            last_baseline = baseline

            draw.rounded_rectangle(
                [left_pad - 8, plot_top - 4, left_pad + plot_w + 4, baseline + 4],
                radius=10,
                fill=_PANEL,
            )
            draw.text((left_pad - 8, panel_top), name, font=f_title, fill=color)

            maxv = _nice_max(max(values) if values else 1)
            for frac in (0.5, 1.0):
                gy = baseline - plot_h * frac
                draw.line([(left_pad - 6, gy), (left_pad + plot_w, gy)], fill=_GRID, width=1)
                draw.text((6, gy - 8), _fmt_value(maxv * frac), font=f_small, fill=_MUTED)

            for i, v in enumerate(values):
                x0 = left_pad + i * slot + bar_off
                h = int((v / maxv) * plot_h) if maxv else 0
                if h < 2 and v > 0:
                    h = 2
                y0 = baseline - h
                draw.rounded_rectangle([x0, y0, x0 + bar_w, baseline], radius=4, fill=color)
                if show_values and v > 0:
                    label = _fmt_value(v)
                    tw = draw.textlength(label, font=f_small)
                    draw.text((x0 + bar_w / 2 - tw / 2, y0 - 17), label, font=f_small, fill=_TEXT)

        for i, lbl in enumerate(x_labels):
            if i % label_step != 0 and i != len(x_labels) - 1:
                continue
            x_center = left_pad + i * slot + slot / 2
            tw = draw.textlength(lbl, font=f_small)
            draw.text((x_center - tw / 2, last_baseline + 8), lbl, font=f_small, fill=_MUTED)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.warning(f"[Stats] Не удалось отрисовать график: {e}")
        return None
