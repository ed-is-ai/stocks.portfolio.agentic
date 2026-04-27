"""
Analyst Agent — scores each stock against CANSLIM / Weinstein Stage 2 criteria.
Uses Foundry Local (phi-4-mini via OpenAI-compatible API) for commentary and
falls back to rule-based scoring.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Iterable

import openai

from ms_agent_framework import Agent
from models import CANSLIMScore, MomentumScore, StockAnalysis, StockRecord


FOUNDRY_BASE_URL = "http://localhost:5272/v1"
FOUNDRY_MODEL = "phi-4-mini"

SCORE_PROMPT = """You are a CANSLIM and Weinstein Stage Analysis momentum expert.

Given the following stock data, return ONLY a valid JSON object (no markdown, no explanation) with:
- \"score\": integer 1-10 (10 = strongest setup)
- \"stage\": one of \"Stage 1\", \"Stage 2\", \"Stage 3\", \"Stage 4\"
- \"near_entry\": boolean (true if within 5% of a valid Stage 2 breakout base)
- \"strengths\": list of up to 3 short bullet strings
- \"risks\": list of up to 2 short bullet strings
- \"summary\": one sentence

Stock data:
{data}

Scoring guide:
- Stage 2 = price above SMA150 and SMA200, SMA200 trending up, price near 52w high
- Strong CANSLIM = RSI 50-80, relative volume > 1.0, within 5-15% of 52w high
- Near entry = price within 5% of recent base breakout point (approximated by 52w high proximity)
"""

RESULTS_DB = str(Path(__file__).parent / "results.db")


def _sma_slope(prices: list[float], window: int, lookback: int = 4) -> float | None:
    """Approximate SMA slope: current window-SMA minus the same SMA 'lookback' steps ago.

    Returns None when there is insufficient data (tests / cold-start).
    Positive = rising, negative = declining.
    Weekly closes are used to approximate the equivalent daily SMA direction.
    """
    if len(prices) < window + lookback:
        return None
    current_sma = sum(prices[-window:]) / window
    past_sma = sum(prices[-(window + lookback):-lookback]) / window
    return current_sma - past_sma


def _stage_classify(stock: StockRecord) -> str:
    """Classify Weinstein stage using SMA levels and slope direction.

    Stage 2 requires SMA200 to be rising — price position alone is not enough.
    Falls back to simple structural classification when price_history is too short.
    """
    price = stock.price
    sma150 = stock.sma150 or price
    sma200 = stock.sma200 or price

    above_150 = price > sma150
    above_200 = price > sma200
    ma_bullish = sma150 > sma200  # 150-day above 200-day = bullish alignment

    s150 = _sma_slope(stock.price_history, window=30, lookback=4)
    s200 = _sma_slope(stock.price_history, window=40, lookback=4)

    # Insufficient history — fall back to structure-only classification
    if s150 is None or s200 is None:
        if above_150 and ma_bullish:
            return "Stage 2"
        if not above_150 and not above_200:
            return "Stage 4"
        if above_200 and not above_150:
            return "Stage 3"  # Price dropped through SMA150, SMA200 still above
        return "Stage 1"  # Price above SMA150 but SMAs not yet aligned

    # Full slope-aware classification
    s200_rising = s200 > 0
    s150_declining = s150 < 0

    # Stage 2: classic advancing — price above aligned, rising SMAs
    if above_150 and ma_bullish and s200_rising:
        return "Stage 2"
    # Stage 4: declining — price below both SMAs and SMA150 is falling
    if not above_150 and not above_200 and s150_declining:
        return "Stage 4"
    # Stage 3: topping — dropped below SMA150 while SMA200 still rising, or
    #           still above SMA150 but SMA200 has started rolling over
    if (not above_150 and above_200) or (above_150 and ma_bullish and not s200_rising):
        return "Stage 3"
    # Stage 1: basing — SMAs flattening, price recovering from Stage 4 lows
    return "Stage 1"


def _open_db() -> sqlite3.Connection:
    """Open (or create) the results database and ensure the schema exists."""
    conn = sqlite3.connect(RESULTS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_history (
            ticker       TEXT NOT NULL,
            as_of        TEXT NOT NULL,
            score        INTEGER NOT NULL,
            canslim_total INTEGER,
            momentum_total INTEGER,
            stage        TEXT,
            price        REAL,
            entry_price  REAL,
            stop_loss    REAL,
            near_entry   INTEGER,
            PRIMARY KEY (ticker, as_of)
        )
    """)
    conn.commit()
    return conn


def _load_previous_scores(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {ticker: last_score} for every ticker that has history."""
    rows = conn.execute("""
        SELECT ticker, score
        FROM analysis_history
        WHERE as_of = (
            SELECT MAX(as_of) FROM analysis_history h2 WHERE h2.ticker = analysis_history.ticker
        )
    """).fetchall()
    return {ticker: score for ticker, score in rows}


def _save_results(conn: sqlite3.Connection, records: list[StockRecord], as_of: str) -> None:
    """Persist this run's analysis results to the database."""
    rows: list[tuple[str, str, int, int | None, int | None, str | None, float | None, float | None, float | None, int]] = []
    for stock in records:
        a = stock.analysis
        if not a:
            continue
        rows.append((
            stock.ticker,
            as_of,
            a.score,
            a.canslim.total if a.canslim else None,
            a.momentum.total if a.momentum else None,
            a.stage,
            stock.price,
            a.entry_price,
            a.stop_loss,
            1 if a.near_entry else 0,
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO analysis_history
           (ticker, as_of, score, canslim_total, momentum_total, stage,
            price, entry_price, stop_loss, near_entry)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


class AnalystAgent(Agent):
    name: str = "AnalystAgent"

    PROGRESS_FILE: ClassVar[str] = str(Path(__file__).parent / "analysis_progress.txt")

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
        conn = _open_db()
        prev_scores = _load_previous_scores(conn)

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
                    canslim = f"{analysis.canslim.total}/14" if analysis.canslim else "—"
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
                record.analysis.canslim.total if record.analysis and record.analysis.canslim else 0,
                record.analysis.momentum.total if record.analysis and record.analysis.momentum else 0,
            ),
            reverse=True,
        )

        _save_results(conn, sorted_results, as_of)
        conn.close()

        self._print_table(sorted_results, prev_scores)
        return sorted_results

    @staticmethod
    def _sparkline(prices: list[float], width: int = 12) -> str:
        """Render a sparkline from a price series using ASCII block chars."""
        bars = " _.,-~^*#"  # 9 levels, ASCII-safe
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
            f"{'Price':>9}  {'Entry':>9}  {'Stop':>9}  {'Risk%':>6}  {'Stage':<8}  {'NE':>2}  Chart"
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
            ne = "Y" if a.near_entry else " "
            delta = _fmt_delta(stock.ticker, a.score, prev_scores)
            if a.entry_price and a.stop_loss:
                risk_pct = f"{(a.entry_price - a.stop_loss) / a.entry_price * 100:.1f}%"
            else:
                risk_pct = "—"
            spark = AnalystAgent._sparkline(stock.price_history)
            print(
                f"  {stock.ticker:<6}  {a.score:>3}/10  {delta:>4}  {canslim:>7}  {mom:>5}  "
                f"{price:>9}  {entry:>9}  {stop:>9}  {risk_pct:>6}  {a.stage:<8}  {ne:>2}  {spark}"
            )

    def get_llm_client(self) -> tuple[openai.OpenAI, str] | None:
        """Return (client, model_id) if Foundry Local is reachable, else None."""
        try:
            client = openai.OpenAI(base_url=FOUNDRY_BASE_URL, api_key="foundry-local")
            models = client.models.list().data
            if not models:
                return None
            # Prefer any phi-4-mini variant; fall back to first available model.
            preferred = next(
                (m.id for m in models if "phi-4-mini" in m.id.lower()), models[0].id
            )
            return client, preferred
        except Exception:
            return None

    def score_stock(
        self, stock: StockRecord, llm: tuple[openai.OpenAI, str] | None
    ) -> StockAnalysis:
        """Score a stock using the LLM if available, else rule-based fallback.

        CANSLIM, momentum, entry price, and stop loss are always computed
        deterministically — the LLM only provides narrative fields.
        """
        if llm:
            analysis = self.score_stock_llm(stock, llm[0], llm[1])
        else:
            analysis = self.rule_based_score(stock)

        if analysis.canslim is None:
            analysis.canslim = self._canslim_fundamental_score(stock)
        if analysis.momentum is None:
            analysis.momentum = self._momentum_score(stock)
        if analysis.entry_price is None:
            base_high = stock.high_base or stock.high_52w
            analysis.entry_price = round(base_high * 1.005, 2)
        if analysis.stop_loss is None:
            base_high = stock.high_base or stock.high_52w
            entry_price = analysis.entry_price or round(base_high * 1.005, 2)
            oneil_stop = round(entry_price * 0.92, 2)
            handle_stop = round((stock.handle_low or 0) * 0.99, 2)
            analysis.stop_loss = max(handle_stop, oneil_stop) if stock.handle_low else oneil_stop

        return analysis

    @staticmethod
    def _strip_think(text: str) -> str:
        """Remove <think>...</think> reasoning blocks emitted by reasoning models."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def score_stock_llm(
        self, stock: StockRecord, client: openai.OpenAI, model: str
    ) -> StockAnalysis:
        """Score via Foundry Local LLM, falling back to rule-based on parse error."""
        data = stock.model_dump(exclude={"price_history", "analysis"})
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

    def rule_based_score(self, stock: StockRecord) -> StockAnalysis:
        """Score purely on CANSLIM fundamentals + momentum technicals (50/50).

        Technical conditions are still evaluated to populate strengths/risks
        text for alert emails, but they do not influence the numeric score.
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

        # Collect qualitative signals for alert email text (no scoring)
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

        near_entry = -5 <= pct_from_high <= 0
        stage = _stage_classify(stock)
        canslim = self._canslim_fundamental_score(stock)
        momentum = self._momentum_score(stock)

        # Score is 50% CANSLIM fundamentals + 50% momentum technicals
        score = max(1, min(10, round(
            0.5 * (canslim.total / 14 * 10) + 0.5 * (momentum.total / 14 * 10)
        )))

        base_high = stock.high_base or stock.high_52w
        entry_price = round(base_high * 1.005, 2)
        oneil_stop = round(entry_price * 0.92, 2)          # O'Neil 8% rule
        handle_stop = round((stock.handle_low or 0) * 0.99, 2)  # just below handle low
        stop_loss = max(handle_stop, oneil_stop) if stock.handle_low else oneil_stop

        return StockAnalysis(
            score=score,
            stage=stage,
            near_entry=near_entry,
            entry_price=entry_price,
            stop_loss=stop_loss,
            canslim=canslim,
            momentum=momentum,
            strengths=strengths[:3],
            risks=risks[:2],
            summary=f"Score {score}/10 (CANSLIM {canslim.total}/14, Mom {momentum.total}/14). {stage}.",
        )

    def _momentum_score(self, stock: StockRecord) -> MomentumScore:
        """Score each letter 0–2 using technical/price proxies for CANSLIM letters."""
        price = stock.price
        sma10 = stock.sma10 or price
        sma30 = stock.sma30 or price
        sma50 = stock.sma50 or price
        sma150 = stock.sma150 or price
        rsi = stock.rsi14 or 50.0
        rel_vol = stock.rel_volume or 1.0
        pct_from_high = stock.pct_from_52w_high or -50.0
        week_chg = stock.pct_change_week or 0.0
        high_52w = stock.high_52w
        low_52w = stock.low_52w

        # C — weekly price change (3% = strong move, 0.5% = modest progress)
        c = 2 if week_chg >= 3 else 1 if week_chg >= 0.5 else 0

        # A — annual strength (position within 52w range)
        rng = high_52w - low_52w
        pct_in_range = (price - low_52w) / rng if rng > 0 else 0.5
        a = 2 if pct_in_range >= 0.8 else 1 if pct_in_range >= 0.6 else 0

        # N — new highs (proximity to 52w high)
        n = 2 if pct_from_high >= -5 else 1 if pct_from_high >= -15 else 0

        # S — supply & demand (relative volume)
        s = 2 if rel_vol >= 1.5 else 1 if rel_vol >= 1.0 else 0

        # L — leader vs laggard (RSI; extend partial credit down to 40 for healthy pullbacks)
        l = 2 if 60 <= rsi <= 80 else 1 if 40 <= rsi < 60 else 0

        # I — institutional proxy: price above SMA150 with rising slope
        #     (independent of Stage to avoid circular dependency)
        s150 = _sma_slope(stock.price_history, window=30, lookback=4)
        s150_rising = s150 is None or s150 > 0  # treat unknown as positive
        i = 2 if price > sma150 and s150_rising else 1 if price > sma150 else 0

        # M — trend alignment (SMA stack)
        if price > sma10 > sma30 > sma50:
            m = 2
        elif price > sma50:
            m = 1
        else:
            m = 0

        return MomentumScore(C=c, A=a, N=n, S=s, L=l, I=i, M=m)

    def _canslim_fundamental_score(self, stock: StockRecord) -> CANSLIMScore:
        """True CANSLIM scoring using fundamental and market data.

        Missing data scores 0 for that letter — no score is awarded when
        the data is unavailable rather than assuming a pass.
        """
        pct_from_high = stock.pct_from_52w_high or -50.0
        rel_vol = stock.rel_volume or 1.0

        # C — Current quarterly EPS growth (≥25% = 2, ≥10% = 1)
        eps = stock.eps_growth
        c = 2 if eps is not None and eps >= 0.25 else 1 if eps is not None and eps >= 0.10 else 0

        # A — Annual earnings quality via ROE (≥17% = 2, ≥10% = 1)
        roe = stock.roe
        a = 2 if roe is not None and roe >= 0.17 else 1 if roe is not None and roe >= 0.10 else 0

        # N — New highs / breakout (same proxy as momentum: 52w high proximity)
        n = 2 if pct_from_high >= -5 else 1 if pct_from_high >= -15 else 0

        # S — Supply & demand via relative volume (same proxy as momentum)
        s = 2 if rel_vol >= 1.5 else 1 if rel_vol >= 1.0 else 0

        # L — Leader: 12m price return vs S&P 500 (outperform by 20pp = 2, any outperform = 1)
        rs = stock.rel_strength_vs_spy
        l = 2 if rs is not None and rs >= 20 else 1 if rs is not None and rs >= 0 else 0

        # I — Institutional sponsorship (≥50% held = 2, ≥30% = 1)
        inst = stock.inst_ownership_pct
        i = 2 if inst is not None and inst >= 0.50 else 1 if inst is not None and inst >= 0.30 else 0

        # M — Market direction: SPY in confirmed uptrend (above 200-day SMA)
        m = 2 if stock.spy_uptrend else 0

        return CANSLIMScore(C=c, A=a, N=n, S=s, L=l, I=i, M=m)


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
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    scan_file = _Path(__file__).parent.parent / "scanner" / "scan_results.json"
    with open(scan_file, encoding="utf-8") as handle:
        scan_data = json.load(handle)

    results = AnalystAgent().run([StockRecord.model_validate(item) for item in scan_data])

    out = _Path(__file__).parent / "analysis_results.json"
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(json.dumps([item.model_dump() for item in results], indent=2))

    print("\nTop 5 setups:")
    for record in results[:5]:
        analysis = record.analysis
        if not analysis:
            continue
        print(
            f"  {record.ticker:6s}  {analysis.score}/10  {analysis.stage:8s} "
            f"near_entry={analysis.near_entry}"
        )
        print(f"           {analysis.summary}")
