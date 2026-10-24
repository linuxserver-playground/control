"""
A shields-style flat badge drawn by hand so the README of every image can show CI state without a third
party in the loop. Text widths are estimated at 6.5px per character of Verdana 11px, which is what shields
does when it has no font metrics either.
"""

from html import escape

COLORS = {"passing": "#3fb950", "failing": "#f85149", "unknown": "#8b949e", "running": "#d29922"}


def text_width(text: str) -> int:
    return int(len(text) * 6.5) + 10


def badge_svg(label: str, message: str, color: str) -> str:
    left = text_width(label)
    right = text_width(message)
    width = left + right
    label_x = left * 5  # text coordinates are scaled by 0.1 for sub-pixel centring
    message_x = round((left + right / 2) * 10)
    title = f"{escape(label)}: {escape(message)}"
    font = "Verdana,Geneva,DejaVu Sans,sans-serif"

    def text(x: int, y: int, length: int, value: str, shadow: bool) -> str:
        fill = 'fill="#010101" fill-opacity=".3" aria-hidden="true"' if shadow else 'fill="#fff"'
        return f'<text x="{x}" y="{y}" {fill} transform="scale(.1)" textLength="{length}">{escape(value)}</text>'

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" aria-label="{title}">',
        f"<title>{title}</title>",
        '<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/>',
        '<stop offset="1" stop-opacity=".1"/></linearGradient>',
        f'<clipPath id="r"><rect width="{width}" height="20" rx="3" fill="#fff"/></clipPath>',
        f'<g clip-path="url(#r)"><rect width="{left}" height="20" fill="#555"/>',
        f'<rect x="{left}" width="{right}" height="20" fill="{color}"/>',
        f'<rect width="{width}" height="20" fill="url(#s)"/></g>',
        f'<g fill="#fff" text-anchor="middle" font-family="{font}" '
        'text-rendering="geometricPrecision" font-size="110">',
        text(label_x, 150, (left - 10) * 10, label, True),
        text(label_x, 140, (left - 10) * 10, label, False),
        text(message_x, 150, (right - 10) * 10, message, True),
        text(message_x, 140, (right - 10) * 10, message, False),
        "</g></svg>",
    ]
    return "\n".join(parts) + "\n"


def ci_badge(result: str | None) -> str:
    """`result` is a builds.result: success → passing, failure/cancelled/timed out → failing, None → unknown."""
    if result is None:
        status = "unknown"
    elif result == "success":
        status = "passing"
    else:
        status = "failing"
    return badge_svg("CI", status, COLORS[status])
