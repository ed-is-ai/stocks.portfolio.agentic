"""Summarise Yau-Weinstein robustness variants against the stored baseline run.

Reads every ``docs/research/robustness/*.json`` written by
``gh140_sleeve_replay.py`` and the stored run 93f205c5 as the baseline.
Costs are 0.2% of each buy and sale's value, charged on its fill day.
"""

import json
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "93f205c5-35ab-4007-afb8-b22ada16264b"
COST = 0.002
FOLDER = ROOT / "docs/research/robustness"


def baseline() -> dict:
    """The stored run in the same shape as a replay result."""
    db = sqlite3.connect(f"file:{ROOT / 'data/backtest.db'}?mode=ro", uri=True)
    curve = [
        {"date": d, "total": v}
        for d, v in db.execute(
            "SELECT date,total_equity_base FROM equity_curve WHERE run_id=? "
            "ORDER BY sequence",
            (BASELINE,),
        )
    ]
    traded: dict[str, float] = defaultdict(float)
    closed = []
    for kind, raw in db.execute(
        "SELECT kind,event_json FROM trade_log WHERE run_id=? AND kind IN "
        "('entry_fill','exit_fill','terminal_settlement')",
        (BASELINE,),
    ):
        event = json.loads(raw)
        if kind == "entry_fill":
            traded[event["fill_session"]] += float(event["cost_base"])
            continue
        if kind == "exit_fill":
            traded[event["fill_session"]] += float(event["proceeds_base"])
        closed.append(
            float(event["realized_pnl_base"]) / float(event["cost_basis_base"])
        )
    return {
        "equity_curve": curve,
        "traded_base_by_day": traded,
        "closed_trade_returns": closed,
    }


def curves(result: dict) -> tuple[list[str], list[float], list[float]]:
    """Dates, gross equity index and equity index net of costs."""
    points = [(p["date"], float(p["total"])) for p in result["equity_curve"]]
    traded = result["traded_base_by_day"]
    gross, net = [1.0], [1.0]
    for (_, v0), (d1, v1) in zip(points, points[1:]):
        r = v1 / v0 - 1
        gross.append(gross[-1] * (1 + r))
        net.append(net[-1] * (1 + r - COST * traded.get(d1, 0.0) / v0))
    return [d for d, _ in points], gross, net


def cagr(dates: list[str], curve: list[float], start: str, end: str) -> float:
    index = [i for i, d in enumerate(dates) if start <= d <= end]
    i, j = index[0], index[-1]
    return (curve[j] / curve[i]) ** (251.5 / (j - i)) - 1


def max_drawdown(curve: list[float]) -> float:
    peak, worst = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def summarise(result: dict) -> dict:
    dates, gross, net = curves(result)
    returns = result["closed_trade_returns"]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    years = (len(dates) - 1) / 251.5
    equity = {p["date"]: float(p["total"]) for p in result["equity_curve"]}
    turnover = sum(v / equity[d] for d, v in result["traded_base_by_day"].items())
    return {
        "gross": cagr(dates, gross, "0", "9"),
        "net": cagr(dates, net, "0", "9"),
        "net_to_2025": cagr(dates, net, "0", "2025-12-31"),
        "net_2016_2025": cagr(dates, net, "2015-12-31", "2025-12-31"),
        "drawdown": max_drawdown(net),
        "trades": len(returns),
        "win_rate": len(wins) / len(returns),
        "payoff": st.mean(wins) / -st.mean(losses),
        "turnover": turnover / years,
    }


def main() -> None:
    rows = {"baseline (stored run)": summarise(baseline())}
    for path in sorted(p for p in FOLDER.glob("*.json") if p.stem != "summary"):
        rows[path.stem] = summarise(json.loads(path.read_text()))
    (FOLDER / "summary.json").write_text(json.dumps(rows, indent=1))
    print(
        f"{'variant':22} {'gross':>6} {'net':>6} {'to2025':>6} {'16-25':>6} "
        f"{'maxDD':>6} {'trades':>6} {'win':>5} {'payoff':>6} {'turn/yr':>7}"
    )
    for name, r in rows.items():
        print(
            f"{name:22} {r['gross']:6.1%} {r['net']:6.1%} {r['net_to_2025']:6.1%} "
            f"{r['net_2016_2025']:6.1%} {r['drawdown']:6.1%} {r['trades']:6} "
            f"{r['win_rate']:5.0%} {r['payoff']:6.2f} {r['turnover']:7.1f}"
        )


if __name__ == "__main__":
    main()
