"""Compare the #140 SPY cash-sleeve replay with the unchanged replay and SPY.

Reads docs/research/gh140/{off,sleeve}.json and SPY total return in GBP from
the price cache. Figures are before trading costs (the engine charges none).
"""

import bisect
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPY = "fa9a29c2d5fcb637620eb38b4254dc5266c2f2573e25613603cd8ffa47c6c3b4"
FX = "cffb8b84b095464bb6b63c7cbd7881fbda26060953425a306f547764be40c00a"


def spy_gbp():
    db = sqlite3.connect(
        f"file:{ROOT / 'data/historical_price_cache.db'}?mode=ro", uri=True
    )
    h = lambda x: float.fromhex(x) if x else 0.0  # noqa: E731
    fx = db.execute(
        "SELECT session_date,close_hex FROM historical_price_observations WHERE data_revision=? ORDER BY 1",
        (FX,),
    ).fetchall()
    fd = [d for d, _ in fx]
    out, tr, prev = {}, 1.0, None
    for d, c, dv in db.execute(
        "SELECT session_date,close_hex,dividends_hex FROM historical_price_observations WHERE data_revision=? ORDER BY 1",
        (SPY,),
    ):
        c, dv = h(c), h(dv)
        if prev:
            tr *= (c + dv) / prev
        prev = c
        out[d] = tr / h(fx[max(0, bisect.bisect_right(fd, d) - 1)][1])
    keys = sorted(out)
    return lambda d: out[keys[bisect.bisect_right(keys, d) - 1]]


def series(arm):
    data = json.loads((ROOT / f"docs/research/gh140/{arm}.json").read_text())
    return [
        (p["date"], float(p["total"]), float(p["sleeve"]), float(p["cash"]))
        for p in data["equity_curve"]
    ]


def cagr(points, a, b):
    sel = [p for p in points if a <= p[0] <= b]
    years = (len(sel) - 1) / 251.5
    return (sel[-1][1] / sel[0][1]) ** (1 / years) - 1


def mdd(values):
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def yearly(points):
    last = {}
    for d, v, *_ in points:
        last[d[:4]] = v
    out, prev = {}, points[0][1]
    for y in sorted(last):
        out[y] = last[y] / prev - 1
        prev = last[y]
    return out


def main():
    g = spy_gbp()
    off, sleeve = series("off"), series("sleeve")
    spx = [(d, g(d) / g(off[0][0]) * off[0][1], 0, 0) for d, *_ in off]
    arms = {"No sleeve": off, "SPY cash sleeve": sleeve, "S&P 500": spx}
    print(
        f"{'arm':16} {'final':>10} {'CAGR':>7} {'to2025':>7} {'00-15':>7} {'16-25':>7} {'maxDD':>7}"
    )
    for n, p in arms.items():
        print(
            f"{n:16} {p[-1][1]:10,.0f} {cagr(p, '0', '9'):7.2%} {cagr(p, '0', '2025-12-31'):7.2%} "
            f"{cagr(p, '0', '2015-12-31'):7.2%} {cagr(p, '2015-12-31', '2025-12-31'):7.2%} {mdd([x[1] for x in p]):7.1%}"
        )
    yo, ys, yx = yearly(off), yearly(sleeve), yearly(spx)
    full = [y for y in yo if y < "2026"]
    print(
        f"years ahead of S&P 500: no sleeve {sum(yo[y] > yx[y] for y in full)}/{len(full)}, "
        f"sleeve {sum(ys[y] > yx[y] for y in full)}/{len(full)}; sleeve beats no-sleeve {sum(ys[y] > yo[y] for y in full)}/{len(full)}"
    )
    print(
        f"mean sleeve share of equity {sum(s / t for _, t, s, _ in sleeve) / len(sleeve):.1%}, "
        f"mean idle cash {sum(c / t for _, t, _, c in sleeve) / len(sleeve):.2%}"
    )
    print("year   nosleeve   sleeve     S&P   sleeve-S&P")
    for y in sorted(yo):
        print(f"{y}  {yo[y]:+8.1%} {ys[y]:+8.1%} {yx[y]:+8.1%} {ys[y] - yx[y]:+8.1%}")


if __name__ == "__main__":
    main()
