"""Draw docs/benchmark-{light,dark}.svg (mean F1 vs cost) from bench/results.json.

    uv run python bench/chart.py

The README shows the two files through a <picture> element so GitHub picks the one that
matches the viewer's theme. Standard library only.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "bench" / "results.json"
HIGHLIGHT = "Jev (batch 20)"  # jevgrep's default configuration
NAMES = {
    "Jev (batch 20)": "Jev · jevgrep default",
    "Jev (batch 20, list layout)": "Jev · list layout",
    "Jev (batch 1)": "Jev · batch 1",
}
# Where each label sits relative to its dot: (dx, dy, text-anchor).
PLACEMENT = {
    "Jev (batch 20)": (0, -28, "middle"),
    "Jev (batch 20, list layout)": (12, -2, "start"),
    "Jev (batch 1)": (-12, 8, "end"),
    "Claude Haiku 4.5": (12, 8, "start"),
    "Claude Sonnet 5": (-12, -8, "end"),
}

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "border": "#e1e0d9",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "accent": "#2a78d6",
        "context": "#898781",
    },
    "dark": {
        "surface": "#1a1a19",
        "border": "#2c2c2a",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "primary": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "accent": "#3987e5",
        "context": "#898781",
    },
}

W, H = 760, 400
LEFT, RIGHT, TOP, BOTTOM = 72, 724, 96, 340  # plot area
X_DECADES = (-3, 0)  # $0.001 .. $1, log scale
Y_RANGE = (0.70, 1.00)
FONT = 'system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif'


def systems() -> list[dict]:
    runs = json.loads(RESULTS.read_text())["runs"]
    out = []
    for name in dict.fromkeys(r["system"] for r in runs):
        mine = [r for r in runs if r["system"] == name]
        out.append(
            {
                "name": name,
                "f1": statistics.mean(r["metrics"]["f1"] for r in mine),
                "cost": sum(r["cost_usd"] for r in mine),
            }
        )
    return out


def x_of(cost: float) -> float:
    lo, hi = X_DECADES
    return LEFT + (math.log10(cost) - lo) / (hi - lo) * (RIGHT - LEFT)


def y_of(f1: float) -> float:
    lo, hi = Y_RANGE
    return BOTTOM - (f1 - lo) / (hi - lo) * (BOTTOM - TOP)


def text(x, y, content, *, fill, size=12, weight=400, anchor="start", halo=None) -> str:
    """A label; `halo` paints a surface-colored outline so gridlines never cut through it."""
    outline = (
        f' stroke="{halo}" stroke-width="4" stroke-linejoin="round" paint-order="stroke"'
        if halo
        else ""
    )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{outline}>{escape(content)}</text>'
    )


def svg(theme: dict[str, str], data: list[dict]) -> str:
    t = theme
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-labelledby="title desc" font-family=\'{FONT}\'>',
        '<title id="title">jevgrep benchmark: accuracy vs. cost</title>',
        '<desc id="desc">'
        + escape(
            "; ".join(f"{s['name']}: mean F1 {s['f1']:.3f}, cost ${s['cost']:.4f}" for s in data)
        )
        + "</desc>",
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="10" fill="{t["surface"]}" '
        f'stroke="{t["border"]}"/>',
        text(24, 36, "Accuracy vs. cost on 195 labelled log lines", fill=t["primary"], size=17,
             weight=600),
        text(24, 60, "Mean F1 over three questions against total cost through OpenRouter "
             "(log scale). Up and to the left is better.", fill=t["secondary"], size=13),
    ]  # fmt: skip

    # Horizontal gridlines and y ticks.
    for f1 in (0.70, 0.80, 0.90, 1.00):
        y = y_of(f1)
        parts.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{RIGHT}" y2="{y:.1f}" '
            f'stroke="{t["axis"] if f1 == Y_RANGE[0] else t["grid"]}" stroke-width="1"/>'
        )
        parts.append(text(LEFT - 10, y + 4, f"{f1:.2f}", fill=t["muted"], size=12, anchor="end"))
    # X ticks, one per decade.
    for decade, label in zip(range(X_DECADES[0], X_DECADES[1] + 1),
                             ("$0.001", "$0.01", "$0.10", "$1.00"), strict=True):  # fmt: skip
        x = x_of(10**decade)
        parts.append(
            f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{BOTTOM}" stroke="{t["grid"]}" '
            'stroke-width="1"/>'
        )
        parts.append(text(x, BOTTOM + 20, label, fill=t["muted"], size=12, anchor="middle"))
    parts.append(
        text((LEFT + RIGHT) / 2, BOTTOM + 44, "Total cost for 585 line judgments (USD, log scale)",
             fill=t["secondary"], size=12, anchor="middle")
    )  # fmt: skip
    parts.append(
        f'<text x="20" y="{(TOP + BOTTOM) / 2:.1f}" fill="{t["secondary"]}" font-size="12" '
        f'text-anchor="middle" transform="rotate(-90 20 {(TOP + BOTTOM) / 2:.1f})">Mean F1</text>'
    )

    # Context points first, the highlighted one last so it sits on top.
    for s in sorted(data, key=lambda s: s["name"] == HIGHLIGHT):
        x, y = x_of(s["cost"]), y_of(s["f1"])
        hot = s["name"] == HIGHLIGHT
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{7 if hot else 5.5}" '
            f'fill="{t["accent"] if hot else t["context"]}" stroke="{t["surface"]}" '
            'stroke-width="2"/>'
        )
        dx, dy, anchor = PLACEMENT.get(s["name"], (12, 4, "start"))
        name = NAMES.get(s["name"], s["name"])
        parts.append(
            text(x + dx, y + dy, name, fill=t["primary"] if hot else t["secondary"], size=13,
                 weight=600 if hot else 500, anchor=anchor, halo=t["surface"])
        )  # fmt: skip
        parts.append(
            text(x + dx, y + dy + 16, f"F1 {s['f1']:.3f} · ${s['cost']:.4f}", fill=t["muted"],
                 size=12, anchor=anchor, halo=t["surface"])
        )  # fmt: skip
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    data = systems()
    for mode, theme in THEMES.items():
        path = ROOT / "docs" / f"benchmark-{mode}.svg"
        path.write_text(svg(theme, data), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
