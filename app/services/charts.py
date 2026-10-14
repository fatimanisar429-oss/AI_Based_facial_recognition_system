"""Builds the numbers for the SVG column chart. Plain Python, no chart library.

The template only draws what this returns, so the geometry is easy to test.
Specs follow the dataviz rules: bars at most 24px thick, 4px rounded tops, square at the baseline,
a 2px gap between neighbouring bars, hairline gridlines.
"""

WIDTH, HEIGHT = 600, 240
LEFT, RIGHT, TOP, BOTTOM = 34, 6, 12, 30
MAX_BAR = 22
BAR_GAP = 2
RADIUS = 4
_NICE_MAXES = (4, 8, 12, 20, 40, 60, 80, 100, 200, 400, 600, 800, 1000, 2000, 4000)


def nice_ceiling(value):
    """Smallest 'round' axis maximum that fits value. All are even, so the middle tick is whole."""
    for candidate in _NICE_MAXES:
        if value <= candidate:
            return candidate
    return -(-value // 1000) * 1000


def _bar_path(x, y, w, h, base):
    """Rectangle with rounded top corners and a flat bottom on the baseline."""
    r = min(RADIUS, h, w / 2)
    return (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{base:.1f} Z")


def column_chart(groups, series_names):
    """groups: [{"label": "Mon", "full": "Mon 5 Oct", "values": [3, 1]}, ...] (one value per series)."""
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = HEIGHT - TOP - BOTTOM
    base = TOP + plot_h
    total = sum(sum(g["values"]) for g in groups)
    axis_max = nice_ceiling(max((v for g in groups for v in g["values"]), default=0))

    band = plot_w / max(len(groups), 1)
    n = len(series_names)
    bar_w = min(MAX_BAR, (band * 0.7 - BAR_GAP * (n - 1)) / n)
    cluster = n * bar_w + (n - 1) * BAR_GAP

    bars, labels = [], []
    for i, g in enumerate(groups):
        start = LEFT + i * band + (band - cluster) / 2
        for s, value in enumerate(g["values"]):
            if value <= 0:
                continue
            h = value / axis_max * plot_h
            x = start + s * (bar_w + BAR_GAP)
            bars.append({
                "path": _bar_path(x, base - h, bar_w, h, base),
                "series": s,
                "tip": f"{g['full']}: {value} {series_names[s].lower()}",
            })
        labels.append({"x": round(LEFT + i * band + band / 2, 1), "text": g["label"]})

    ticks = [{"y": round(base - plot_h * f, 1), "text": str(int(axis_max * f))} for f in (0, 0.5, 1)]
    return {
        "width": WIDTH, "height": HEIGHT, "left": LEFT, "right": WIDTH - RIGHT, "base": base,
        "bar_width": bar_w, "ticks": ticks, "bars": bars, "labels": labels, "total": total,
        "series": series_names, "groups": groups,
    }
