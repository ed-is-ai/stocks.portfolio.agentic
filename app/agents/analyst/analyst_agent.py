"""
Analyst Agent — scores each stock against CANSLIM / Weinstein Stage 2 criteria.

SEPA assessment calls the vcp-screener calculator modules directly:
  - calculate_trend_template  → 7-point Minervini trend template
  - calculate_vcp_pattern     → contraction detection, pivot price
  - calculate_volume_pattern  → dry-up ratio, breakout volume
  - calculate_pivot_proximity → stop-loss below last contraction low
  - compute_execution_state   → actionable state (Pre-breakout, Breakout, …)

Falls back to rule-based inline logic when ohlcv_history is absent.
"""

import json
import re
import sys
from datetime import datetime
from typing import ClassVar, Iterable

import openai

from app.agents.analyst.historical_pivots import find_historical_pivots
from app.agents.base import Agent
from app.core.config import ANALYSIS_JSON, ANALYSIS_PROGRESS_TXT, SCAN_RESULTS_JSON
from app.core.config import RESULTS_DB as _RESULTS_DB_PATH
from app.core.config import SKILLS_DIR
from app.repositories import db
from app.repositories.results_repo import ResultRow, ResultsRepository
from app.schemas import CANSLIMScore, MomentumScore, StockAnalysis, StockRecord

_VCP_SCRIPTS = str(SKILLS_DIR / "vcp-screener" / "scripts")
_BTP_SCRIPTS = str(SKILLS_DIR / "breakout-trade-planner" / "scripts")

FOUNDRY_BASE_URL = "http://localhost:5272/v1"
FOUNDRY_MODEL = "phi-4-mini"

SCORE_PROMPT = """You are a CANSLIM and Weinstein Stage Analysis momentum expert trained in
Mark Minervini's VCP (Volatility Contraction Pattern) methodology.

Given the following stock data, return ONLY a valid JSON object (no markdown, no explanation) with:
- "score": integer 1-10 (10 = strongest setup)
- "stage": one of "Stage 1", "Stage 2", "Stage 3", "Stage 4"
- "entry_zone": one of "broken_out"|"approaching"|"getting_close"|"extended"|"far"
- "strengths": list of up to 3 short bullet strings
- "risks": list of up to 2 short bullet strings
- "summary": one sentence

Stock data:
{data}

Scoring guide:
- Stage 2 = price above SMA150 and SMA200, SMA200 trending up, price near 52w high
- Strong CANSLIM = RSI 50-80, relative volume > 1.0, within 5-15% of 52w high
- VCP bonus: valid_vcp_contractions + volume_dry_up = tightest, lowest-risk setup
- entry_zone: broken_out = 0-5% above pivot; approaching = 0-3% below;
  getting_close = 3-8% below; extended = >5% above; far = >8% below
- Execution states: Breakout/Pre-breakout = best entries; Extended/Overextended = avoid chasing;
  Damaged = stop already breached, do not buy
"""

RESULTS_DB = str(_RESULTS_DB_PATH)
_results_repo = ResultsRepository(db.make_connect(lambda: RESULTS_DB))


def _run_vcp_analysis(stock: StockRecord) -> dict | None:
    """Call the vcp-screener calculators using the stock's daily OHLCV history.

    Returns a dict with keys vcp, trend_template, volume, pivot, execution,
    or None when ohlcv_history is absent.

    Imports are lazy so the module loads even when the skills directory is absent.
    """
    if not stock.ohlcv_history:
        return None

    if _VCP_SCRIPTS not in sys.path:
        sys.path.insert(0, _VCP_SCRIPTS)

    from calculators.execution_state import compute_execution_state  # type: ignore[import]
    from calculators.pivot_proximity_calculator import calculate_pivot_proximity  # type: ignore[import]
    from calculators.trend_template_calculator import calculate_trend_template  # type: ignore[import]
    from calculators.vcp_pattern_calculator import calculate_vcp_pattern  # type: ignore[import]
    from calculators.volume_pattern_calculator import calculate_volume_pattern  # type: ignore[import]

    ohlcv = stock.ohlcv_history
    quote = {"price": stock.price, "yearHigh": stock.high_52w, "yearLow": stock.low_52w}

    # Estimate RS rank from rel_strength_vs_spy so criterion 7 is scored
    rs = stock.rel_strength_vs_spy
    rs_rank: int | None = None
    if rs is not None:
        rs_rank = 80 if rs >= 20 else 71 if rs >= 0 else 50

    tt_result = calculate_trend_template(ohlcv, quote, rs_rank=rs_rank)

    vcp_result = calculate_vcp_pattern(ohlcv)

    pivot_price = vcp_result.get("pivot_price")
    contractions = vcp_result.get("contractions", [])
    last_low: float | None = contractions[-1].get("low_price") if contractions else None

    vol_result = calculate_volume_pattern(
        ohlcv, pivot_price=pivot_price, contractions=contractions
    )

    piv_result = calculate_pivot_proximity(
        current_price=stock.price,
        pivot_price=pivot_price,
        last_contraction_low=last_low,
        breakout_volume=vol_result.get("breakout_volume_detected", False),
    )

    sma200 = tt_result.get("sma200")
    sma200_dist = (stock.price - sma200) / sma200 * 100 if sma200 else None

    exec_result = compute_execution_state(
        distance_from_pivot_pct=piv_result.get("distance_from_pivot_pct"),
        price=stock.price,
        sma50=tt_result.get("sma50"),
        sma200=sma200,
        sma200_distance_pct=sma200_dist,
        last_contraction_low=last_low,
        breakout_volume=vol_result.get("breakout_volume_detected", False),
    )

    return {
        "vcp": vcp_result,
        "trend_template": tt_result,
        "volume": vol_result,
        "pivot": piv_result,
        "execution": exec_result,
    }


def _run_btp_pricing(pivot: float, last_contraction_low: float) -> dict | None:
    """Compute trade prices via the breakout-trade-planner risk_calculator.

    Returns signal_entry, worst_entry, stop_loss, r_multiples (1/2/3R), and
    risk_pct (as a decimal fraction). Returns None on any import or calc error.
    """
    try:
        sys.path.insert(0, _BTP_SCRIPTS)
        from risk_calculator import (  # type: ignore[import]
            calculate_r_multiples,
            calculate_risks,
            derive_trade_prices,
        )

        signal_entry, worst_entry, stop_loss = derive_trade_prices(
            pivot, last_contraction_low
        )
        r_multiples: dict[str, float] = calculate_r_multiples(signal_entry, stop_loss)
        risk_pct_signal, _ = calculate_risks(signal_entry, worst_entry, stop_loss)
        return {
            "signal_entry": signal_entry,
            "worst_entry": worst_entry,
            "stop_loss": stop_loss,
            "r_multiples": r_multiples,
            "risk_pct": risk_pct_signal / 100.0,
        }
    except Exception:
        return None


def _sma_slope(prices: list[float], window: int, lookback: int = 4) -> float | None:
    """Approximate SMA slope from weekly price history. Positive = rising."""
    if len(prices) < window + lookback:
        return None
    current_sma = sum(prices[-window:]) / window
    past_sma = sum(prices[-(window + lookback) : -lookback]) / window
    return current_sma - past_sma


def _hh_hl_structure(prices: list[float], window: int = 4, periods: int = 3) -> bool:
    """Return True when weekly closes show consecutive higher highs + higher lows.

    Splits the most recent (window × periods) weeks into equal windows and checks
    that each successive window's max > prior max (HH) AND min > prior min (HL).
    """
    needed = window * periods
    if len(prices) < needed:
        return False
    recent = prices[-needed:]
    windows = [recent[i * window : (i + 1) * window] for i in range(periods)]
    highs = [max(w) for w in windows]
    lows = [min(w) for w in windows]
    hh = all(highs[i] > highs[i - 1] for i in range(1, periods))
    hl = all(lows[i] > lows[i - 1] for i in range(1, periods))
    return hh and hl


def _vol_confirms_price(ohlcv: list[dict], lookback: int = 20) -> bool:
    """Return True when average up-day volume exceeds average down-day volume.

    Compares the last *lookback* daily bars. An up day is close > open.
    Returns False (conservative) when ohlcv is empty.
    """
    recent = ohlcv[:lookback]
    if not recent:
        return False
    up_vols = [int(d["volume"]) for d in recent if float(d["close"]) > float(d["open"])]
    dn_vols = [
        int(d["volume"]) for d in recent if float(d["close"]) <= float(d["open"])
    ]
    if not up_vols or not dn_vols:
        return bool(up_vols)
    return sum(up_vols) / len(up_vols) > sum(dn_vols) / len(dn_vols)


def _ma_compressed(sma50: float, sma150: float, threshold: float = 0.02) -> bool:
    """Return True when SMA50 and SMA150 are within *threshold* of each other.

    Converging MAs signal a tight base forming before a potential breakout.
    """
    if sma150 <= 0:
        return False
    return abs(sma50 - sma150) / sma150 < threshold


def _stage_classify(stock: StockRecord) -> str:
    """Classify Weinstein stage using SMA levels and slope direction."""
    price = stock.price
    sma150 = stock.sma150 or price
    sma200 = stock.sma200 or price

    above_150 = price > sma150
    above_200 = price > sma200
    ma_bullish = sma150 > sma200

    s150 = _sma_slope(stock.price_history, window=30, lookback=4)
    s200 = _sma_slope(stock.price_history, window=40, lookback=4)

    if s150 is None or s200 is None:
        if above_150 and ma_bullish:
            return "Stage 2"
        if not above_150 and not above_200:
            return "Stage 4"
        if above_200 and not above_150:
            return "Stage 3"
        return "Stage 1"

    if above_150 and ma_bullish and s200 > 0:
        return "Stage 2"
    if not above_150 and not above_200 and s150 < 0:
        return "Stage 4"
    if (not above_150 and above_200) or (above_150 and ma_bullish and s200 <= 0):
        return "Stage 3"
    return "Stage 1"


def _sepa_assessment(stock: StockRecord, vcp: dict | None) -> dict[str, bool]:
    """Evaluate SEPA template: 8 Minervini + 4 VCP + 3 technical-analyst criteria.

    When vcp analysis is available the trend template criteria come directly
    from calculate_trend_template(); VCP criteria from calculate_vcp_pattern()
    and calculate_volume_pattern(). Falls back to inline SMA arithmetic when
    ohlcv_history is absent.

    Technical-analyst additions (always computed from StockRecord):
        hh_hl_uptrend      — HH/HL structure over last 12 weekly closes
        vol_confirms_price — avg up-day volume > avg down-day volume (last 20 days)
        ma_compressed      — SMA50 and SMA150 within 2% (tight base forming)
    """
    if vcp:
        tt = vcp["trend_template"]
        criteria = tt.get("criteria", {})

        def _passed(key: str) -> bool:
            return bool(criteria.get(key, {}).get("passed", False))

        def _sma200_rising() -> bool:
            if _passed("c3_sma200_trending_up"):
                return True
            detail = criteria.get("c3_sma200_trending_up", {}).get("detail", "")
            if "Cannot verify" in detail or "Insufficient data" in detail:
                slope = _sma_slope(stock.price_history, window=40, lookback=4)
                return slope is not None and slope > 0
            return False

        sma50_val = tt.get("sma50") or stock.sma50 or stock.price
        sma150_val = tt.get("sma150") or stock.sma150 or stock.price
        sma200_val = tt.get("sma200") or stock.sma200 or stock.price

        vcp_r = vcp["vcp"]
        vol_r = vcp["volume"]
        piv_r = vcp["pivot"]

        result: dict[str, bool] = {
            # 7-point trend template from calculator
            "above_150_200": _passed("c1_price_above_sma150_200"),
            "sma150_above_200": _passed("c2_sma150_above_sma200"),
            "sma200_rising": _sma200_rising(),
            "above_sma50": _passed("c4_price_above_sma50"),
            "above_25pct_of_low": _passed("c5_25pct_above_52w_low"),
            "within_25pct_of_high": _passed("c6_within_25pct_52w_high"),
            "rs_leader": _passed("c7_rs_rank_above_70"),
            # Extra Minervini condition from returned SMA values
            "sma50_above_150_200": sma50_val > sma150_val and sma50_val > sma200_val,
            # VCP criteria
            "vcp_contractions": vcp_r.get("num_contractions", 0) >= 2,
            "volume_dry_up": (vol_r.get("dry_up_ratio") or 1.0) < 1.0,
            "tight_action": not vcp_r.get("wide_and_loose", True),
            "near_pivot": (piv_r.get("distance_from_pivot_pct") or -999.0) >= -8.0,
        }
    else:
        # ---- fallback: inline SMA arithmetic (no ohlcv_history) ----
        price = stock.price
        sma50_val = stock.sma50 or price
        sma150_val = stock.sma150 or price
        sma200_val = stock.sma200 or price
        s200 = _sma_slope(stock.price_history, window=40, lookback=4)

        result = {
            "above_150_200": price > sma150_val and price > sma200_val,
            "sma150_above_200": sma150_val > sma200_val,
            "sma200_rising": s200 is not None and s200 > 0,
            "sma50_above_150_200": sma50_val > sma150_val and sma50_val > sma200_val,
            "above_25pct_of_low": price >= stock.low_52w * 1.25,
            "within_25pct_of_high": stock.pct_from_52w_high >= -25,
            "rs_leader": (stock.rel_strength_vs_spy or -999) >= 0,
            "above_sma50": price > sma50_val,
            # VCP criteria — approximate from weekly data
            "vcp_contractions": False,
            "volume_dry_up": (stock.rel_volume or 1.0) < 1.0,
            "tight_action": stock.atr14 is not None and stock.atr14 < price * 0.02,
            "near_pivot": stock.high_base is not None
            and price >= stock.high_base * 0.92,
        }

    # Technical-analyst framework additions — same regardless of VCP data availability
    result["hh_hl_uptrend"] = _hh_hl_structure(stock.price_history)
    result["vol_confirms_price"] = _vol_confirms_price(stock.ohlcv_history)
    result["ma_compressed"] = _ma_compressed(sma50_val, sma150_val)
    return result


def _execution_state_to_entry_zone(state: str) -> str:
    """Map VCP execution state to the legacy entry_zone string."""
    _map = {
        "Breakout": "broken_out",
        "Early-post-breakout": "broken_out",
        "Pre-breakout": "approaching",
        "Extended": "extended",
        "Overextended": "extended",
        "Damaged": "far",
        "Invalid": "far",
    }
    return _map.get(state, "far")


def _compute_vcp_stop(
    entry_price: float,
    handle_low: float | None,
    sma50: float | None,
    max_risk_pct: float = 0.15,
) -> float:
    """Fallback stop loss when the pivot proximity calculator has no pivot.

    Priority: handle_low → SMA50 → hard cap at max_risk_pct below entry.
    """
    if handle_low and handle_low < entry_price:
        stop = round(handle_low * 0.99, 2)
    elif sma50 and sma50 < entry_price:
        stop = round(sma50 * 0.99, 2)
    else:
        stop = round(entry_price * (1 - max_risk_pct), 2)

    if (entry_price - stop) / entry_price > max_risk_pct:
        stop = round(entry_price * (1 - max_risk_pct), 2)
    return stop


def _derive_rr(btp: dict, vcp: dict | None) -> float:
    """Derive reward:risk from the distance between VCP pivot points.

    Reward = T1 high (top of base) minus entry.
    Risk   = entry minus stop.
    Falls back to 2.0 when fewer than two contractions exist or the
    base high is at or below entry (stock already extended).
    """
    entry = btp["signal_entry"]
    stop = btp["stop_loss"]
    risk = entry - stop
    if risk <= 0:
        return 2.0

    contractions = vcp["vcp"].get("contractions", []) if vcp and vcp.get("vcp") else []
    if len(contractions) >= 2:
        base_high = contractions[0]["high_price"]
        reward = base_high - entry
        if reward > 0:
            return round(reward / risk, 1)

    return 2.0


def _save_results(records: list[StockRecord], as_of: str) -> None:
    """Persist this run's analysis results via the results repository."""
    rows: list[ResultRow] = []
    for stock in records:
        a = stock.analysis
        if not a:
            continue
        rows.append(
            (
                stock.ticker,
                as_of,
                a.score,
                a.canslim.total if a.canslim else None,
                a.momentum.total if a.momentum else None,
                a.stage,
                stock.price,
                a.entry_price,
                a.stop_loss,
                a.entry_zone,
            )
        )
    _results_repo.save_results(rows)


class AnalystAgent(Agent):
    name: str = "AnalystAgent"

    PROGRESS_FILE: ClassVar[str] = str(ANALYSIS_PROGRESS_TXT)

    def run(self, payload: Iterable[StockRecord]) -> list[StockRecord]:
        scan_results = list(payload)
        llm = self.get_llm_client()
        mode_line = (
            f"Using Foundry Local ({llm[1]}) for analysis"
            if llm
            else "Foundry Local unavailable — using rule-based scoring"
        )
        print(mode_line)

        as_of = datetime.now().strftime("%Y-%m-%d %H:%M")
        _results_repo.ensure_schema()
        prev_scores = _results_repo.latest_scores()

        total = len(scan_results)
        analysis_results: list[StockRecord] = []
        with open(self.PROGRESS_FILE, "w", encoding="utf-8") as progress:
            progress.write(f"{mode_line}\n")
            progress.write(f"Analysing {total} tickers...\n\n")
            progress.flush()

            for idx, stock in enumerate(scan_results, 1):
                try:
                    analysis = self.score_stock(stock, llm)
                    stock.analysis = analysis
                    analysis_results.append(stock)
                    canslim = (
                        f"{analysis.canslim.total}/14" if analysis.canslim else "—"
                    )
                    mom = f"{analysis.momentum.total}/14" if analysis.momentum else "—"
                    delta = _fmt_delta(stock.ticker, analysis.score, prev_scores)
                    line = (
                        f"[{idx:>3}/{total}] {stock.ticker:<6}  "
                        f"score={analysis.score}/10 {delta}  CANSLIM={canslim}  Mom={mom}  "
                        f"{analysis.stage}"
                    )
                except Exception as error:
                    line = f"[{idx:>3}/{total}] {stock.ticker:<6}  ERROR: {error}"
                    print(f"  [err] {stock.ticker}: {error}")

                progress.write(line + "\n")
                progress.flush()

        sorted_results = sorted(
            analysis_results,
            key=lambda record: (
                record.analysis.score if record.analysis else 0,
                record.analysis.canslim.total
                if record.analysis and record.analysis.canslim
                else 0,
                record.analysis.momentum.total
                if record.analysis and record.analysis.momentum
                else 0,
            ),
            reverse=True,
        )

        _save_results(sorted_results, as_of)

        self._print_table(sorted_results, prev_scores)
        return sorted_results

    @staticmethod
    def _sparkline(prices: list[float], width: int = 12) -> str:
        """Render a sparkline from a price series using ASCII block chars."""
        bars = " _.,-~^*#"
        if len(prices) < 2:
            return " " * width
        step = max(1, len(prices) // width)
        sampled = [prices[i] for i in range(0, len(prices), step)][-width:]
        lo, hi = min(sampled), max(sampled)
        span = hi - lo or 1
        return "".join(bars[round((v - lo) / span * 8)] for v in sampled)

    @staticmethod
    def _print_table(records: list[StockRecord], prev_scores: dict[str, int]) -> None:
        header = (
            f"  {'Ticker':<6}  {'Scr':>4}  {'Chg':>4}  {'CANSLIM':>7}  {'Mom':>5}  "
            f"{'Price':>9}  {'Entry':>9}  {'Stop':>9}  {'Risk%':>6}  {'Stage':<8}  {'Zone':<10}  "
            f"{'B':>3}  {'S':>3}  {'Net':>4}  {'Rec':<12}  Chart"
        )
        print()
        print(header)
        print("  " + "-" * (len(header) - 2))
        for stock in records:
            a = stock.analysis
            if not a:
                continue
            price = f"${stock.price:,.2f}"
            entry = f"${a.entry_price:,.2f}" if a.entry_price else "—"
            stop = f"${a.stop_loss:,.2f}" if a.stop_loss else "—"
            canslim = f"{a.canslim.total}/14" if a.canslim else "—"
            mom = f"{a.momentum.total}/14" if a.momentum else "—"
            delta = _fmt_delta(stock.ticker, a.score, prev_scores)
            risk_pct = (
                f"{(a.entry_price - a.stop_loss) / a.entry_price * 100:.1f}%"
                if a.entry_price and a.stop_loss
                else "—"
            )
            buyers = str(stock.funds_buying) if stock.funds_buying is not None else "—"
            sellers = (
                str(stock.funds_selling) if stock.funds_selling is not None else "—"
            )
            net = (
                f"+{stock.funds_net}"
                if stock.funds_net and stock.funds_net > 0
                else str(stock.funds_net)
                if stock.funds_net is not None
                else "—"
            )
            rec = recommendation(a)
            spark = AnalystAgent._sparkline(stock.price_history)
            print(
                f"  {stock.ticker:<6}  {a.score:>3}/10  {delta:>4}  {canslim:>7}  {mom:>5}  "
                f"{price:>9}  {entry:>9}  {stop:>9}  {risk_pct:>6}  {a.stage:<8}  {a.entry_zone:<10}  "
                f"{buyers:>3}  {sellers:>3}  {net:>4}  {rec:<12}  {spark}"
            )

    def get_llm_client(self) -> tuple[openai.OpenAI, str] | None:
        """Return (client, model_id) if Foundry Local is reachable, else None."""
        try:
            client = openai.OpenAI(base_url=FOUNDRY_BASE_URL, api_key="foundry-local")
            models = client.models.list().data
            if not models:
                return None
            preferred = next(
                (m.id for m in models if "phi-4-mini" in m.id.lower()), models[0].id
            )
            return client, preferred
        except Exception:
            return None

    def score_stock(
        self, stock: StockRecord, llm: tuple[openai.OpenAI, str] | None
    ) -> StockAnalysis:
        """Score a stock; VCP analysis from the vcp-screener calculators drives
        SEPA template, execution state, entry price, and stop loss.
        """
        vcp = _run_vcp_analysis(stock)

        if llm:
            analysis = self.score_stock_llm(stock, llm[0], llm[1])
        else:
            analysis = self.rule_based_score(stock, vcp)

        if analysis.canslim is None:
            analysis.canslim = self._canslim_fundamental_score(stock)
        if analysis.momentum is None:
            analysis.momentum = self._momentum_score(stock)

        # Entry, stop, R-multiples via breakout-trade-planner risk_calculator
        # Only use VCP pivot when the pattern is confirmed valid; otherwise high_base
        vcp_pivot = (
            vcp["vcp"].get("pivot_price")
            if (vcp and vcp["vcp"].get("valid_vcp"))
            else None
        )
        pivot = vcp_pivot or stock.high_base
        last_low = stock.handle_low or stock.sma50
        btp = _run_btp_pricing(pivot, last_low) if pivot and last_low else None

        if btp:
            analysis.entry_price = btp["signal_entry"]
            analysis.stop_loss = btp["stop_loss"]
            analysis.risk_pct = btp["risk_pct"]
            analysis.r_multiples = btp["r_multiples"]
            analysis.reward_risk_ratio = _derive_rr(btp, vcp)
            contractions = (
                vcp["vcp"].get("contractions", []) if vcp and vcp.get("vcp") else []
            )
            if len(contractions) >= 2:
                analysis.prev_entry_price = contractions[0]["high_price"]
        elif analysis.entry_price is None:
            base = pivot or stock.high_base
            analysis.entry_price = round(base * 1.005, 2) if base else None
            if analysis.entry_price and analysis.stop_loss is None:
                analysis.stop_loss = _compute_vcp_stop(
                    analysis.entry_price, stock.handle_low, stock.sma50
                )

        # Execution state → entry zone (only trust VCP state when pattern is valid)
        vcp_state = (
            vcp["execution"]["state"]
            if (vcp and vcp["vcp"].get("valid_vcp"))
            else _fallback_execution_state(stock)
        )
        analysis.entry_zone = _execution_state_to_entry_zone(vcp_state)

        analysis.volume_confirmed = stock.rel_volume >= 1.4
        analysis.sepa_template = _sepa_assessment(stock, vcp)

        # Multi-year base breakout detection
        myb = _detect_multiyear_breakout(stock.ticker, stock.price)
        if myb:
            analysis.multiyear_breakout = True
            analysis.multiyear_pivot = myb["pivot_price"]
            base_wks = myb["base_weeks"]
            analysis.strengths = list(analysis.strengths) + [
                f"Multi-year base breakout: cleared ${myb['pivot_price']:.2f} "
                f"pivot ({base_wks}w base)"
            ]

        return analysis

    @staticmethod
    def _strip_think(text: str) -> str:
        """Remove <think>...</think> reasoning blocks emitted by reasoning models."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def score_stock_llm(
        self, stock: StockRecord, client: openai.OpenAI, model: str
    ) -> StockAnalysis:
        """Score via Foundry Local LLM, falling back to rule-based on parse error."""
        data = stock.model_dump(exclude={"price_history", "analysis", "ohlcv_history"})
        prompt = SCORE_PROMPT.format(data=json.dumps(data, indent=2))
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=600,
        )
        raw = self._strip_think(response.choices[0].message.content.strip())
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        payload = json.loads(raw)
        return StockAnalysis.model_validate(payload)

    def rule_based_score(
        self, stock: StockRecord, vcp: dict | None = None
    ) -> StockAnalysis:
        """Score on CANSLIM fundamentals + momentum technicals (50/50).

        VCP bonus (+1) when valid_vcp and volume dry-up are both confirmed.
        Strengths/risks text is populated from both Minervini conditions and VCP output.
        """
        strengths: list[str] = []
        risks: list[str] = []

        price = stock.price
        sma50 = stock.sma50 or price
        sma150 = stock.sma150 or price
        sma200 = stock.sma200 or price
        rsi = stock.rsi14 or 50
        rel_vol = stock.rel_volume or 1.0
        pct_from_high = stock.pct_from_52w_high or -50

        if price > sma150 > sma200:
            strengths.append("Price above SMA150 & SMA200 (Stage 2 structure)")
        if price > sma50:
            strengths.append("Price above SMA50")
        if -15 <= pct_from_high <= -1:
            strengths.append(f"Within {abs(pct_from_high):.0f}% of 52w high")
        if 50 <= rsi <= 80:
            strengths.append(f"RSI {rsi:.0f} — healthy momentum range")
        elif rsi > 80:
            risks.append(f"RSI {rsi:.0f} — potentially overbought")
        elif rsi < 40:
            risks.append(f"RSI {rsi:.0f} — weak momentum")
        if rel_vol >= 1.5:
            strengths.append(f"Relative volume {rel_vol:.1f}x — institutional interest")
        elif rel_vol < 0.7:
            risks.append(f"Low relative volume ({rel_vol:.1f}x)")
        if pct_from_high < -30:
            risks.append(f"Far from 52w high ({pct_from_high:.0f}%)")

        sepa = _sepa_assessment(stock, vcp)
        if sepa["sma50_above_150_200"]:
            strengths.append("SMA50 above SMA150 & SMA200 (full Minervini alignment)")
        else:
            risks.append("SMA50 not leading — partial trend alignment only")
        if not sepa["within_25pct_of_high"]:
            risks.append("More than 25% below 52w high — outside Minervini template")
        if sepa["above_25pct_of_low"]:
            strengths.append(">=25% above 52w low")

        # VCP-specific signals
        vcp_valid = sepa["vcp_contractions"]
        vol_dry_up = sepa["volume_dry_up"]
        if vcp_valid and vol_dry_up:
            num = vcp["vcp"]["num_contractions"] if vcp else "2+"
            strengths.append(f"VCP: {num} contractions + volume dry-up — ideal setup")
        elif vcp_valid:
            num = vcp["vcp"]["num_contractions"] if vcp else "2+"
            strengths.append(f"VCP: {num} contractions detected")
        if sepa["tight_action"]:
            strengths.append("Price tightening (ATR < 2% of price)")
        if sepa["hh_hl_uptrend"]:
            strengths.append("HH/HL structure confirmed (genuine uptrend)")
        if sepa["vol_confirms_price"]:
            strengths.append("Up-day volume > down-day volume (accumulation)")
        else:
            risks.append("Down-day volume heavier — possible distribution")
        if sepa["ma_compressed"]:
            strengths.append("SMA50 ≈ SMA150 — MAs compressing into tight base")

        stage = _stage_classify(stock)
        canslim = self._canslim_fundamental_score(stock)
        momentum = self._momentum_score(stock)

        base_score = round(
            0.5 * (canslim.total / 14 * 10) + 0.5 * (momentum.total / 14 * 10)
        )
        vcp_bonus = 1 if (vcp_valid and vol_dry_up) else 0
        score = max(1, min(10, base_score + vcp_bonus))

        # Entry price from VCP pivot; stop from pivot proximity calculator
        pivot = vcp["vcp"].get("pivot_price") if vcp else None
        base = pivot or stock.high_base
        entry_price = round(base * 1.005, 2) if base else None

        if vcp and entry_price:
            stop_loss = vcp["pivot"].get("stop_loss_price") or _compute_vcp_stop(
                entry_price, stock.handle_low, stock.sma50
            )
        elif entry_price:
            stop_loss = _compute_vcp_stop(entry_price, stock.handle_low, stock.sma50)
        else:
            stop_loss = None

        vcp_state = (
            vcp["execution"]["state"] if vcp else _fallback_execution_state(stock)
        )
        entry_zone = _execution_state_to_entry_zone(vcp_state)

        return StockAnalysis(
            score=score,
            stage=stage,
            entry_zone=entry_zone,
            entry_price=entry_price,
            stop_loss=stop_loss,
            volume_confirmed=rel_vol >= 1.4,
            sepa_template=sepa,
            canslim=canslim,
            momentum=momentum,
            strengths=strengths[:3],
            risks=risks[:2],
            summary=(
                f"Score {score}/10 (CANSLIM {canslim.total}/14, Mom {momentum.total}/14). "
                f"{stage}. VCP state: {vcp_state}."
            ),
        )

    def _momentum_score(self, stock: StockRecord) -> MomentumScore:
        """Score each letter 0–2 using technical/price proxies for CANSLIM letters."""
        price = stock.price
        sma10 = stock.sma10 or price
        sma30 = stock.sma30 or price
        sma50 = stock.sma50 or price
        sma150 = stock.sma150 or price
        sma200 = stock.sma200 or price
        rsi = stock.rsi14 or 50.0
        rel_vol = stock.rel_volume or 1.0
        pct_from_high = stock.pct_from_52w_high or -50.0
        week_chg = stock.pct_change_week or 0.0
        high_52w = stock.high_52w
        low_52w = stock.low_52w

        c = 2 if week_chg >= 3 else 1 if week_chg >= 0.5 else 0

        rng = high_52w - low_52w
        pct_in_range = (price - low_52w) / rng if rng > 0 else 0.5
        pct_above_low = (price - low_52w) / low_52w * 100 if low_52w > 0 else 0.0
        a = (
            2
            if pct_in_range >= 0.8 and pct_above_low >= 25
            else 1
            if pct_in_range >= 0.6
            else 0
        )

        n = 2 if pct_from_high >= -5 else 1 if pct_from_high >= -15 else 0
        s = 2 if rel_vol >= 1.5 else 1 if rel_vol >= 1.0 else 0
        l = 2 if 60 <= rsi <= 80 else 1 if 40 <= rsi < 60 else 0  # noqa: E741

        s150 = _sma_slope(stock.price_history, window=30, lookback=4)
        s150_rising = s150 is None or s150 > 0
        i = 2 if price > sma150 and s150_rising else 1 if price > sma150 else 0

        if price > sma10 > sma30 > sma50 and sma50 > sma150 and sma50 > sma200:
            m = 2
        elif price > sma50:
            m = 1
        else:
            m = 0

        return MomentumScore(C=c, A=a, N=n, S=s, L=l, I=i, M=m)

    def _canslim_fundamental_score(self, stock: StockRecord) -> CANSLIMScore:
        """True CANSLIM scoring using fundamental and market data."""
        pct_from_high = stock.pct_from_52w_high or -50.0
        rel_vol = stock.rel_volume or 1.0

        eps = stock.eps_growth
        c = (
            2
            if eps is not None and eps >= 0.25
            else 1
            if eps is not None and eps >= 0.10
            else 0
        )

        annual_eps = stock.annual_eps_growth
        roe = stock.roe
        if annual_eps is not None:
            a = 2 if annual_eps >= 0.25 else 1 if annual_eps >= 0.15 else 0
        else:
            a = (
                2
                if roe is not None and roe >= 0.17
                else 1
                if roe is not None and roe >= 0.10
                else 0
            )

        n = 2 if pct_from_high >= -5 else 1 if pct_from_high >= -15 else 0
        s = 2 if rel_vol >= 1.5 else 1 if rel_vol >= 1.0 else 0

        rs = stock.rel_strength_vs_spy
        l = 2 if rs is not None and rs >= 20 else 1 if rs is not None and rs >= 0 else 0  # noqa: E741

        inst = stock.inst_ownership_pct
        i = (
            2
            if inst is not None and inst >= 0.50
            else 1
            if inst is not None and inst >= 0.30
            else 0
        )

        m = 2 if stock.spy_uptrend else 0

        return CANSLIMScore(C=c, A=a, N=n, S=s, L=l, I=i, M=m)


def _fallback_execution_state(stock: StockRecord) -> str:
    """Simple execution state when no ohlcv_history — uses StockRecord SMA fields."""
    price = stock.price
    sma50 = stock.sma50
    sma200 = stock.sma200
    pivot = stock.high_base
    handle_low = stock.handle_low

    if sma50 and sma200 and price < sma50 and sma50 < sma200:
        return "Invalid"
    if handle_low and price < handle_low:
        return "Damaged"
    if not pivot:
        return "Invalid"
    pct = (price - pivot) / pivot * 100
    if sma200 and sma200 > 0 and price > sma200 * 1.50:
        return "Overextended"
    if pct > 10.0:
        return "Overextended"
    if pct > 5.0:
        return "Extended"
    if pct > 3.0:
        return "Early-post-breakout"
    if pct >= 0.0:
        return "Breakout"
    if pct >= -8.0:
        return "Pre-breakout"
    return "far"


_MYB_ABOVE_MAX = 8.0  # price must be no more than 8% above the pivot
_MYB_MIN_BASE_WEEKS = 26  # base must have lasted at least 26 weeks


def _detect_multiyear_breakout(ticker: str, price: float) -> dict | None:
    """Return the most significant historical pivot that price has just cleared.

    Looks for a confirmed or resistance pivot where price is 0–8% above the
    pivot level and the base lasted ≥26 weeks. Returns None on any error.
    """
    try:
        pivots = find_historical_pivots(ticker, require_stage2=False)
    except Exception:
        return None

    candidates = []
    for p in pivots:
        if p.get("status") == "open":
            continue
        pct_above = (price - p["pivot_price"]) / p["pivot_price"] * 100
        if (
            0.0 <= pct_above <= _MYB_ABOVE_MAX
            and p["base_weeks"] >= _MYB_MIN_BASE_WEEKS
        ):
            candidates.append((pct_above, p))

    if not candidates:
        return None

    # Prefer the pivot with the longest base (most significant resistance)
    return max(candidates, key=lambda x: x[1]["base_weeks"])[1]


def recommendation(a: StockAnalysis) -> str:
    """Return buy-side recommendation (never SELL — use ExitEvaluator for exits)."""
    if a.stage in ("Stage 3", "Stage 4") or a.score <= 3:
        return "Avoid"
    if a.stage == "Stage 2":
        if a.entry_zone == "broken_out" and a.score >= 7:
            return "BUY" if a.volume_confirmed else "BUY (low vol)"
        if a.entry_zone == "extended":
            return "Extended"
        if a.entry_zone == "approaching" and a.score >= 5:
            return "Stay Alert"
        if a.entry_zone == "getting_close":
            return "Watch"
    return "No Action"


def _fmt_delta(ticker: str, score: int, prev_scores: dict[str, int]) -> str:
    """Return a score-change label: '+2', '-1', '=', or 'new'."""
    if ticker not in prev_scores:
        return "new"
    diff = score - prev_scores[ticker]
    if diff > 0:
        return f"+{diff}"
    if diff < 0:
        return str(diff)
    return "="


if __name__ == "__main__":
    with open(SCAN_RESULTS_JSON, encoding="utf-8") as handle:
        scan_data = json.load(handle)

    results = AnalystAgent().run(
        [StockRecord.model_validate(item) for item in scan_data]
    )

    out = ANALYSIS_JSON
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(json.dumps([item.model_dump() for item in results], indent=2))

    print("\nTop 5 setups:")
    for record in results[:5]:
        analysis = record.analysis
        if not analysis:
            continue
        print(
            f"  {record.ticker:6s}  {analysis.score}/10  {analysis.stage:8s} "
            f"zone={analysis.entry_zone}"
        )
        print(f"           {analysis.summary}")
