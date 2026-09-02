"""
Alert Agent — sends email notifications when a stock scores above threshold.
Reads analysis results and fires alerts for near-entry setups.
"""

import json
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Iterable

from pydantic import PrivateAttr

from app.agents.alert.templating import email_templates, get_macro
from app.agents.base import Agent
from app.core.alerting import classify_alert, is_on_cooldown
from app.core.alerting import BREAKOUT_COOLDOWN_HOURS as ALERT_COOLDOWN_HOURS
from app.core import config
from app.core.config import ALERTS_DB, ANALYSIS_JSON
from app.schemas import (
    AlertSummary,
    CANSLIMScore,
    EmailConfig,
    MarketNarrative,
    Position,
    StockRecord,
)
from app.schemas.notification import NotificationCategory, NotificationSeverity
from app.schemas.portfolio_recommendation import (
    EvaluationCoverageV1,
    RecommendationResultV1,
)
from app.repositories import db
from app.repositories.alerts_repo import AlertsRepository
from app.repositories.db import Connect
from app.repositories.notifications_repo import (
    NotificationsRepository,
    build_notifications_repository,
)
from app.repositories.position_state_repo import (
    PositionStateRepository,
    build_position_state_repository,
)

DB_PATH = str(ALERTS_DB)


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with safe whitespace handling."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value.strip())
    except (ValueError, AttributeError):
        return default


EMAIL_CONFIG = EmailConfig(
    host=os.getenv("EMAIL_HOST", "smtp.gmail.com"),
    port=_env_int("EMAIL_PORT", 587),
    user=os.getenv("EMAIL_USER", ""),
    password=os.getenv("EMAIL_PASSWORD", ""),
    recipient=os.getenv("EMAIL_TO", ""),
)

# ── CTA copy (#168) ─────────────────────────────────────────────────────
# Always shown, right where the old "How to read" legend (#163) sat —
# after the narrative/snapshot, immediately before the alert sections —
# never gated on content existing unlike the section bodies themselves.
# Single source of truth for wording so text/HTML can't drift; each
# renderer supplies its own markup only.
#
# One group per actual section, in the same order, using the same icons
# and colours as the section <h3> headers below, so the CTA reads as a
# table of contents for what follows. Colours are not new — they're the
# exact hex values already used on the real section headers and
# throughout the stop-loss/risk markup elsewhere in this file: #c0392b
# (red) for sell/stopped-out, #27ae60 (green) for buy, #b45309 (amber)
# for approaching/watch. "TRACKED SETUP — STOPPED OUT" is folded into
# "Sell / stop loss (held)" rather than given its own line: both only
# fire for held tickers (the #113 gating in send_summary_email), so both
# mean "a held name's risk trigger fired — review for exit." "Entry
# reached" and "Breaking out now" are folded into one "Buy" group and
# the two sections themselves are merged (see send_summary_email) — the
# specific reason for each ticker is on its own row/card, not restated
# here.
_CTA_GROUPS: list[tuple[str, str, str, str]] = [
    # (icon, heading, colour, description)
    (
        "⚠",
        "Sell / stop loss (held)",
        "#c0392b",
        "a position you own (or a tracked setup on a name you hold) hit "
        "its stop or target — review for exit.",
    ),
    (
        "⚡",
        "Buy",
        "#27ae60",
        "a setup crossed its pivot, or a fresh breakout just fired — see "
        "the reason on each row below.",
    ),
    (
        "👀",
        "Watch",
        "#b45309",
        "approaching entry — on the radar, not yet at its pivot — add to "
        "your watchlist and wait for the trigger.",
    ),
]
_CTA_NO_ALERTS = "No actionable tickers today — nothing below needs a decision."
# Reuses the exact wording already used app-wide, see
# app/schemas/market_narrative.py: MarketNarrative.not_advice default.
_CTA_NOT_ADVICE = "Informational only — not financial advice."

# Narrative shown on a Buy card for a watchlist setup that crossed its entry
# price on a run where the screener produced no full analysis for it (#356).
# The card still renders every field the Approaching Entry card does, with the
# analysis-derived rows explicitly marked unavailable rather than omitted.
_ANALYSIS_UNAVAILABLE = "Full breakout analysis was not available this run."
_BLANK_BREAKOUT_NARRATIVE = {
    "volume": _ANALYSIS_UNAVAILABLE,
    "sepa": _ANALYSIS_UNAVAILABLE,
    "canslim": _ANALYSIS_UNAVAILABLE,
    "momentum": _ANALYSIS_UNAVAILABLE,
    "verdict": (
        "Watchlist setup crossed its configured entry price. " + _ANALYSIS_UNAVAILABLE
    ),
}


#: Most degraded security ids rendered inline before they are summarised —
#: a wide universe must not turn one diagnostic into an unbounded wall.
_MAX_RENDERED_DEGRADED = 10


def _recommendation_coverage_lines(
    coverage: EvaluationCoverageV1,
) -> list[str]:
    """Return the plain-text evidence warning for one evaluation (#471).

    Each claim is made per path: holdings only fail safe to Hold when the
    exit path was unsupported, and Buy signals are only reported as
    withheld when the entry path was.
    """
    lines: list[str] = []
    missing = tuple(
        dict.fromkeys(coverage.entry_missing_evidence + coverage.exit_missing_evidence)
    )
    if not coverage.entry_supported or not coverage.exit_supported:
        detail = f" ({', '.join(missing)})" if missing else ""
        blocked = (
            "entry and exit"
            if not (coverage.entry_supported or coverage.exit_supported)
            else "exit"
            if not coverage.exit_supported
            else "entry"
        )
        sentence = (
            "WARNING: Evidence incomplete — this Strategy's "
            f"{blocked} rules could not be evaluated{detail}."
        )
        if not coverage.exit_supported:
            sentence += " Holdings fail safe to Hold."
        if not coverage.entry_supported:
            sentence += " No buy signals were produced."
        lines.append(sentence)
    if coverage.degraded_securities:
        shown = coverage.degraded_securities[:_MAX_RENDERED_DEGRADED]
        extra = len(coverage.degraded_securities) - len(shown)
        suffix = f", +{extra} more" if extra > 0 else ""
        lines.append(
            "WARNING: Evidence incomplete — some securities lacked enough "
            f"evidenced history and were not evaluated ({', '.join(shown)}"
            f"{suffix})."
        )
    lines.append("")
    return lines


class AlertAgent(Agent):
    name: str = "AlertAgent"
    db_path: str = DB_PATH
    email_config: EmailConfig = EMAIL_CONFIG

    _buy_alerts: list[tuple[StockRecord, str]] = PrivateAttr(default_factory=list)
    _sell_alerts: list[tuple[Position, StockRecord | None]] = PrivateAttr(
        default_factory=list
    )
    # Watched-setup signals folded into the per-run digest (#81), not emailed
    # individually. Each tuple is (ticker, current_price, level, stock) —
    # ``stock`` is the current run's analysed StockRecord when the ticker was
    # present in that run's scan, else None (#312: retain analysis instead of
    # discarding it so tracked tiles can render real CANSLIM data).
    _entry_triggered: list[tuple[str, float, float, StockRecord | None]] = PrivateAttr(
        default_factory=list
    )
    _watched_stops: list[tuple[str, float, float, StockRecord | None]] = PrivateAttr(
        default_factory=list
    )
    _alerts: AlertsRepository = PrivateAttr()
    _notifications: NotificationsRepository = PrivateAttr()
    _position_state: PositionStateRepository = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        connect: Connect = db.make_connect(lambda: self.db_path)
        self._alerts = AlertsRepository(connect)
        self._notifications = build_notifications_repository()
        self._position_state = build_position_state_repository()

    def _notify(
        self,
        event_type: str,
        title: str,
        *,
        severity: NotificationSeverity = NotificationSeverity.INFO,
        body: str = "",
        ticker: str | None = None,
    ) -> None:
        """Record an alert notification, never letting it break email flow.

        Watched-setup events are recorded only when the digest is actually
        dispatched (mirrors "alerts sent"), whereas held-portfolio critical
        events are recorded unconditionally so they are never lost (#81).
        """
        try:
            self._notifications.record(
                NotificationCategory.ALERT,
                event_type,
                title,
                severity=severity,
                body=body,
                ticker=ticker,
            )
        except Exception as error:  # pragma: no cover - defensive
            print(f"  [notify] error: {error}")

    def check_positions(self, stocks: list[StockRecord]) -> None:
        """Queue watched-setup entry/stop signals for the per-run digest.

        These watchlist candidates are folded into ``send_summary_email`` (#81)
        rather than emailed individually. The DB status transitions
        (``entered``/``stopped``) still fire so rows don't re-alert.
        """
        self._entry_triggered.clear()
        self._watched_stops.clear()
        rows = self._alerts.watching()
        if not rows:
            return
        stock_map = {s.ticker: s for s in stocks}
        for rowid, ticker, entry_price, stop_loss in rows:
            stock = stock_map.get(ticker)
            current = stock.price if stock else None
            if current is None:
                continue
            if entry_price is not None and current >= entry_price:
                print(
                    f"\nENTRY TRIGGERED: {ticker} @ ${current:.2f} "
                    f"(entry was ${entry_price:.2f})"
                )
                self._entry_triggered.append((ticker, current, entry_price, stock))
                self._alerts.set_status(rowid, "entered")
            elif stop_loss is not None and current <= stop_loss:
                print(
                    f"\nSTOP LOSS HIT: {ticker} @ ${current:.2f} "
                    f"(stop was ${stop_loss:.2f})"
                )
                self._watched_stops.append((ticker, current, stop_loss, stock))
                self._alerts.set_status(rowid, "stopped")

    def run(self, payload: Iterable[StockRecord]) -> AlertSummary:
        results = list(payload)
        self._alerts.ensure_schema()
        self._alerts.clear_terminal()
        self.check_positions(results)
        self._buy_alerts.clear()

        for stock in results:
            if not self.should_alert(stock):
                continue
            trigger = self.alert_trigger(stock)
            assert trigger is not None
            print(f"\nALERT: {stock.ticker} [{trigger}]")
            self._buy_alerts.append((stock, trigger))
            self.record_alert(stock)

        print(f"\n{len(self._buy_alerts)} buy alert(s) queued.")
        return AlertSummary(
            buy_count=len(self._buy_alerts),
            tickers=[stock.ticker for stock, _ in self._buy_alerts],
        )

    def init_db(self) -> None:
        """Ensure the alerts schema exists (kept for backward compatibility)."""
        self._alerts.ensure_schema()

    def was_recently_alerted(
        self, ticker: str, cooldown_hours: int = ALERT_COOLDOWN_HOURS
    ) -> bool:
        """Return True if ``ticker`` is watching or within ``cooldown_hours``.

        ``cooldown_hours`` varies by alert kind (see
        ``app.core.alerting.classify_alert``) — breakouts use a short
        cooldown, Stay Alert candidates a much longer one.
        """
        if self._alerts.has_watching(ticker):
            return True
        return is_on_cooldown(self._alerts.last_alerted_at(ticker), cooldown_hours)

    def record_alert(self, stock: StockRecord) -> None:
        assert stock.analysis is not None
        self._alerts.record(
            stock.ticker,
            stock.analysis.score,
            stock.analysis.stage,
            stock.analysis.summary,
            stock.analysis.entry_price,
            stock.analysis.stop_loss,
        )

    def alert_trigger(self, stock: StockRecord) -> str | None:
        """Return a trigger label if this stock warrants an alert, else None.

        Delegates to ``app.core.alerting.classify_alert`` — the single
        source of truth shared with the watchlist UI (#58). Fires for:
          - fresh_breakout: VCP entry_zone just transitioned to broken_out
          - multiyear_breakout: price cleared a significant multi-year base
            pivot
          - Stay Alert candidates: Stage 2, approaching entry, score >= 5
            (emailed at lower priority — see ``cooldown_hours``)
        """
        if not stock.analysis:
            return None
        return classify_alert(stock).label

    @staticmethod
    def _breakout_narrative(stock: StockRecord) -> dict[str, str]:
        """Return a dict of labelled narrative lines assessing breakout strength.

        Keys: volume, sepa, canslim, momentum, verdict
        Values: plain-text sentences ready for both text and HTML rendering.
        """
        a = stock.analysis
        assert a is not None

        # --- Volume ---
        rv = stock.rel_volume
        if rv >= 1.5:
            vol_line = (
                f"Strong volume confirmation ({rv:.1f}x average) — institutions buying."
            )
        elif rv >= 1.0:
            vol_line = f"Moderate volume ({rv:.1f}x average) — acceptable but watch for follow-through."
        else:
            vol_line = f"Weak volume ({rv:.1f}x average) — breakout lacks conviction; wait for volume surge."

        # --- SEPA ---
        sepa_count = 0
        if a.sepa_template:
            t = a.sepa_template
            checks = {
                "P>SMA150&200": t.get("above_150_200"),
                "SMA150>200": t.get("sma150_above_200"),
                "SMA200 rising": t.get("sma200_rising"),
                "SMA50>150&200": t.get("sma50_above_150_200"),
                ">25% above low": t.get("above_25pct_of_low"),
                "<25% from high": t.get("within_25pct_of_high"),
                "RS leader": t.get("rs_leader"),
                "P>SMA50": t.get("above_sma50"),
            }
            sepa_count = sum(1 for v in checks.values() if v)
            failed = [k for k, v in checks.items() if not v]
            if sepa_count == 8:
                sepa_line = (
                    "Perfect SEPA trend template (8/8) — textbook Stage 2 uptrend."
                )
            elif sepa_count >= 6:
                missing = ", ".join(failed)
                sepa_line = f"Strong SEPA ({sepa_count}/8). Missing: {missing}."
            else:
                missing = ", ".join(failed)
                sepa_line = f"Weak SEPA ({sepa_count}/8) — trend template incomplete. Missing: {missing}."
        else:
            sepa_line = "SEPA data unavailable."

        # --- CANSLIM ---
        if a.canslim:
            cs = a.canslim
            parts = []
            if cs.C == 2:
                parts.append("strong current earnings (C=2)")
            elif cs.C == 1:
                parts.append("moderate current earnings (C=1)")
            else:
                parts.append("weak/missing current earnings (C=0)")
            if cs.A == 2:
                parts.append("strong annual earnings growth (A=2)")
            elif cs.A == 0:
                parts.append("weak/missing annual earnings (A=0)")
            if cs.L == 2:
                parts.append("RS leader vs S&P (L=2)")
            elif cs.L == 0:
                parts.append("lagging vs S&P (L=0)")
            if cs.I == 2:
                parts.append("high institutional ownership (I=2)")
            elif cs.I == 0:
                parts.append("low institutional ownership (I=0)")
            m_str = "market in uptrend (M=2)" if cs.M == 2 else "market headwind (M<2)"
            canslim_line = f"CANSLIM {cs.total}/14: {'; '.join(parts)}; {m_str}."
        else:
            canslim_line = "CANSLIM data unavailable."

        # --- Momentum ---
        if a.momentum:
            mo = a.momentum
            mom_parts = []
            if mo.N == 2:
                mom_parts.append("at 52w high (N=2)")
            if mo.L == 2:
                mom_parts.append("RSI leader (L=2)")
            elif mo.L == 0:
                mom_parts.append("RSI weak (L=0)")
            if mo.I == 2:
                mom_parts.append("Stage 2 SMA structure (I=2)")
            if mo.S == 2:
                mom_parts.append("elevated volume (S=2)")
            elif mo.S == 0:
                mom_parts.append("low volume (S=0)")
            mom_line = (
                f"Momentum {mo.total}/14: {'; '.join(mom_parts)}."
                if mom_parts
                else f"Momentum {mo.total}/14."
            )
        else:
            mom_line = "Momentum data unavailable."

        # --- Verdict ---
        score = a.score
        signals_strong = rv >= 1.5 and sepa_count >= 6 and score >= 8
        signals_ok = rv >= 1.0 and sepa_count >= 5 and score >= 7
        if signals_strong:
            verdict = "HIGH CONVICTION — volume, trend template, and score all confirm. Consider acting promptly."
        elif signals_ok:
            verdict = "MODERATE CONVICTION — setup is valid but not all signals align. Size position conservatively."
        else:
            verdict = "LOW CONVICTION — one or more key signals are weak. Wait for stronger confirmation before entry."

        return {
            "volume": vol_line,
            "sepa": sepa_line,
            "canslim": canslim_line,
            "momentum": mom_line,
            "verdict": verdict,
        }

    def should_alert(self, stock: StockRecord) -> bool:
        """Return True if ``stock`` is email-eligible and not on cooldown."""
        classification = classify_alert(stock)
        if not classification.email_eligible:
            return False
        if self.was_recently_alerted(stock.ticker, classification.cooldown_hours):
            print(f"  [skip] {stock.ticker}: already alerted recently")
            return False
        return True

    @staticmethod
    def _canslim_text(canslim: CANSLIMScore | None) -> str:
        """Plain-text CANSLIM line: total + all 7 components, or unavailable.

        Single source of truth shared by every plain-text tile (#312) so text
        can't drift from the HTML ``canslim_block`` macro.
        """
        if canslim is None:
            return "CANSLIM: unavailable"
        return (
            f"CANSLIM {canslim.total}/14: "
            f"C={canslim.C} A={canslim.A} N={canslim.N} S={canslim.S} "
            f"L={canslim.L} I={canslim.I} M={canslim.M}"
        )

    @staticmethod
    def _congress_summary(stock: StockRecord) -> str | None:
        """Net congressional trades + Senate breakdown, or None when uncovered.

        Mirrors the watchlist's presence/net rule: rendered only when either
        congressional count is present; ``net = buys - sells``.
        """
        if stock.congress_buys is None and stock.congress_sells is None:
            return None
        buys = stock.congress_buys or 0
        sells = stock.congress_sells or 0
        net = buys - sells
        return (
            f"Congress: {net:+d} net ({buys} buys / {sells} sells, 12mo) "
            f"| Senate: {stock.senate_buys or 0} buys / "
            f"{stock.senate_sells or 0} sells"
        )

    def format_alert_text(self, stock: StockRecord, trigger: str | None = None) -> str:
        analysis = stock.analysis
        assert analysis is not None
        strengths = "\n".join(f"  + {item}" for item in analysis.strengths)
        risks = "\n".join(f"  - {item}" for item in analysis.risks)

        entry = f"${analysis.entry_price:.2f}" if analysis.entry_price else "—"
        stop = f"${analysis.stop_loss:.2f}" if analysis.stop_loss else "—"
        if analysis.entry_price and analysis.stop_loss:
            risk_pct = (
                (analysis.entry_price - analysis.stop_loss) / analysis.entry_price
            ) * 100
            risk_str = f"{risk_pct:.1f}% below entry"
        else:
            risk_str = "—"

        canslim_str = f"\n{self._canslim_text(analysis.canslim)}"

        n = self._breakout_narrative(stock)
        trigger_line = f"** {trigger} **\n\n" if trigger else ""
        congress = self._congress_summary(stock)
        congress_line = f"{congress}\n" if congress else ""
        body = (
            f"{trigger_line}{stock.ticker} — Score {analysis.score}/10 | {analysis.stage}\n"
            f"Price: ${stock.price}  |  RSI: {stock.rsi14}  |  RelVol: {stock.rel_volume}x\n"
            f"Distance from 52w high: {stock.pct_from_52w_high}%\n"
            f"{congress_line}"
            f"Entry: {entry}  |  Stop: {stop}  |  Risk: {risk_str}\n"
            f"\n{analysis.summary}\n"
            f"{canslim_str}\n"
            f"\n--- Breakout Assessment ---\n"
            f"Volume:   {n['volume']}\n"
            f"SEPA:     {n['sepa']}\n"
            f"CANSLIM:  {n['canslim']}\n"
            f"Momentum: {n['momentum']}\n"
            f"\n>>> {n['verdict']}\n"
            f"\n{strengths}"
        )
        if risks:
            body += f"\n{risks}"
        body += f"\n\n{date.today().isoformat()}"
        return body

    def format_alert_html(self, stock: StockRecord, trigger: str | None = None) -> str:
        analysis = stock.analysis
        assert analysis is not None
        if analysis.score >= 8:
            score_color = "#27ae60"
        elif analysis.score >= 6:
            score_color = "#f39c12"
        else:
            score_color = "#e74c3c"

        entry = f"${analysis.entry_price:.2f}" if analysis.entry_price else "—"
        stop = f"${analysis.stop_loss:.2f}" if analysis.stop_loss else "—"
        if analysis.entry_price and analysis.stop_loss:
            risk_pct = (
                (analysis.entry_price - analysis.stop_loss) / analysis.entry_price
            ) * 100
            risk_str = f"{risk_pct:.1f}%"
        else:
            risk_str = "—"

        congress = self._congress_summary(stock)
        congress_net_color = "#555"
        if congress:
            net = (stock.congress_buys or 0) - (stock.congress_sells or 0)
            congress_net_color = (
                "#27ae60" if net > 0 else "#c0392b" if net < 0 else "#555"
            )

        vol_color, sepa_color, verdict_bg, verdict_border = self._narrative_colors(
            stock
        )

        return email_templates.get_template("alert.html").render(
            stock=stock,
            analysis=analysis,
            trigger=trigger,
            score_color=score_color,
            strengths=analysis.strengths,
            risks=analysis.risks,
            entry=entry,
            stop=stop,
            risk_str=risk_str,
            congress=congress,
            congress_net_color=congress_net_color,
            narrative=self._breakout_narrative(stock),
            vol_color=vol_color,
            sepa_color=sepa_color,
            verdict_bg=verdict_bg,
            verdict_border=verdict_border,
            svg_chart=self._svg_chart(stock.price_history, score_color),
            today=date.today().isoformat(),
        )

    @staticmethod
    def _svg_chart(prices: list[float], line_color: str = "#2980b9") -> str:
        """Return an inline SVG line chart of weekly closes over the past 12 months."""
        if len(prices) < 2:
            return ""
        w, h, pad = 520, 120, 10
        lo, hi = min(prices), max(prices)
        span = hi - lo or 1
        n = len(prices)

        def x(i: int) -> float:
            return pad + (i / (n - 1)) * (w - 2 * pad)

        def y(v: float) -> float:
            return h - pad - ((v - lo) / span) * (h - 2 * pad)

        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(prices))
        # Filled area under the line
        area_pts = f"{x(0):.1f},{h - pad} " + points + f" {x(n - 1):.1f},{h - pad}"

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'style="display:block;margin:16px 0;font-family:Arial,sans-serif">'
            f'<rect width="{w}" height="{h}" fill="#f8f8f8" rx="4"/>'
            f'<polygon points="{area_pts}" fill="{line_color}" opacity="0.12"/>'
            f'<polyline points="{points}" fill="none" stroke="{line_color}" '
            f'stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{x(n - 1):.1f}" cy="{y(prices[-1]):.1f}" r="3" fill="{line_color}"/>'
            f'<text x="{x(0)}" y="{h - 1}" font-size="9" fill="#aaa">52w ago</text>'
            f'<text x="{x(n - 1) - 20}" y="{h - 1}" font-size="9" fill="#aaa">now</text>'
            f'<text x="{pad + 2}" y="{y(hi) + 9}" font-size="9" fill="#888">'
            f"${hi:,.2f}</text>"
            f'<text x="{pad + 2}" y="{y(lo) - 3}" font-size="9" fill="#888">'
            f"${lo:,.2f}</text>"
            f"</svg>"
        )

    @staticmethod
    def _narrative_colors(stock: StockRecord) -> tuple[str, str, str, str]:
        """Return (vol_color, sepa_color, verdict_bg, verdict_border) for the
        breakout-assessment box rendered in ``alert.html``.
        """
        rv = stock.rel_volume
        vol_color = "#27ae60" if rv >= 1.5 else "#f39c12" if rv >= 1.0 else "#e74c3c"

        a = stock.analysis
        sepa_count = 0
        if a and a.sepa_template:
            sepa_count = sum(1 for v in a.sepa_template.values() if v)
        sepa_color = (
            "#27ae60"
            if sepa_count >= 6
            else "#f39c12"
            if sepa_count >= 4
            else "#e74c3c"
        )

        score = a.score if a else 0
        verdict_bg = (
            "#d4edda"
            if score >= 8 and rv >= 1.5
            else "#fff3cd"
            if score >= 7
            else "#f8d7da"
        )
        verdict_border = (
            "#27ae60"
            if score >= 8 and rv >= 1.5
            else "#f39c12"
            if score >= 7
            else "#e74c3c"
        )
        return vol_color, sepa_color, verdict_bg, verdict_border

    @staticmethod
    def _stop_loss_narrative(
        pos: Position, stock: StockRecord | None
    ) -> dict[str, str]:
        """Assess the situation when a portfolio stop loss is hit."""
        price = pos.current_price or 0.0
        stop = pos.stop_loss or 0.0
        entry = pos.entry_price or pos.avg_cost

        # --- How far through the stop ---
        pct_below = (stop - price) / stop * 100 if stop else 0.0
        if pct_below <= 1.0:
            stop_line = (
                f"Price ${price:.2f} is just touching the stop ${stop:.2f} "
                f"({pct_below:.1f}% below). Could be a false break — watch closely."
            )
        elif pct_below <= 4.0:
            stop_line = (
                f"Price ${price:.2f} is {pct_below:.1f}% below stop ${stop:.2f}. "
                f"Stop is clearly violated — exit discipline required."
            )
        else:
            stop_line = (
                f"Price ${price:.2f} is {pct_below:.1f}% below stop ${stop:.2f}. "
                f"Hard stop breach — exit immediately to limit further damage."
            )

        # --- P&L context ---
        if entry and entry > 0:
            pnl_pct = (price - entry) / entry * 100
            if pnl_pct >= 20:
                pnl_line = (
                    f"Position is still +{pnl_pct:.1f}% from entry ${entry:.2f}. "
                    f"Consider trailing stop rather than full exit."
                )
            elif pnl_pct >= 0:
                pnl_line = (
                    f"Position is +{pnl_pct:.1f}% from entry ${entry:.2f}. "
                    f"Small gain — protect capital, exit at stop."
                )
            else:
                pnl_line = (
                    f"Position is {pnl_pct:.1f}% from entry ${entry:.2f}. "
                    f"Taking a loss — cut quickly to preserve capital for better setups."
                )
        else:
            pnl_line = "Entry price not recorded."

        # --- Volume / selling pressure ---
        if stock:
            rv = stock.rel_volume
            if rv >= 1.5:
                vol_line = (
                    f"Heavy volume ({rv:.1f}x average) on the decline — institutional selling. "
                    f"High probability this is a genuine breakdown, not noise."
                )
            elif rv >= 1.0:
                vol_line = (
                    f"Moderate volume ({rv:.1f}x average). Watch whether volume increases "
                    f"— escalating volume confirms breakdown."
                )
            else:
                vol_line = (
                    f"Low volume ({rv:.1f}x average) on the decline — may be a thin-market "
                    f"false break. Reassess on higher-volume session."
                )
        else:
            vol_line = "Volume data unavailable."

        # --- Stage / trend condition ---
        if stock and stock.analysis:
            a = stock.analysis
            stage = a.stage
            sepa_count = sum(1 for v in (a.sepa_template or {}).values() if v)
            if stage in ("Stage 3", "Stage 4"):
                trend_line = (
                    f"Stock has moved into {stage} — trend is deteriorating. "
                    f"SEPA {sepa_count}/8. Exit aligns with stage analysis."
                )
            elif sepa_count <= 4:
                trend_line = (
                    f"SEPA trend template is weak ({sepa_count}/8) — key Stage 2 "
                    f"conditions are breaking down. Exit is appropriate."
                )
            else:
                trend_line = (
                    f"{stage} structure still intact (SEPA {sepa_count}/8). "
                    f"Stop hit may be temporary — monitor for recovery above stop."
                )
        else:
            trend_line = "Stage/trend data unavailable."

        # --- CANSLIM thesis ---
        if stock and stock.analysis and stock.analysis.canslim:
            cs = stock.analysis.canslim
            broken = []
            if cs.C == 0:
                broken.append("current earnings deteriorating (C=0)")
            if cs.A == 0:
                broken.append("annual earnings weak (A=0)")
            if cs.L == 0:
                broken.append("no longer an RS leader (L=0)")
            if cs.M < 2:
                broken.append("market not in confirmed uptrend (M<2)")
            if broken:
                canslim_line = f"CANSLIM thesis weakening: {'; '.join(broken)}."
            else:
                canslim_line = (
                    f"CANSLIM fundamentals still solid ({cs.total}/14). "
                    f"Stop hit may be technical, not fundamental."
                )
        else:
            canslim_line = "CANSLIM data unavailable."

        # --- Action verdict ---
        if stock and stock.analysis:
            a = stock.analysis
            sepa_count = sum(1 for v in (a.sepa_template or {}).values() if v)
            stage_bad = a.stage in ("Stage 3", "Stage 4")
            vol_bad = stock.rel_volume >= 1.2
            if stage_bad or (pct_below > 4.0) or (vol_bad and sepa_count <= 4):
                verdict = "EXIT NOW — multiple signals confirm breakdown. Do not wait for a bounce."
            elif pct_below <= 1.0 and sepa_count >= 6:
                verdict = (
                    "WATCH — marginal stop touch on good trend template. "
                    "Set a hard intraday alert; exit on close below stop."
                )
            else:
                verdict = (
                    "EXIT — stop is violated. Honour your risk rules. "
                    "Re-evaluate if price reclaims stop on strong volume."
                )
        else:
            verdict = "EXIT — stop loss hit. Honour your risk management rules."

        return {
            "stop": stop_line,
            "pnl": pnl_line,
            "volume": vol_line,
            "trend": trend_line,
            "canslim": canslim_line,
            "verdict": verdict,
        }

    def _format_stop_loss_text(
        self, pos: Position, stock: StockRecord | None, n: dict[str, str]
    ) -> str:
        price = pos.current_price or 0.0
        stop = pos.stop_loss or 0.0
        pnl = pos.unrealised_pnl
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "--"
        pnl_pct = pos.unrealised_pnl_pct
        pnl_pct_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "--"
        canslim = stock.analysis.canslim if stock and stock.analysis else None
        return (
            f"** STOP LOSS HIT: {pos.ticker} **\n\n"
            f"{pos.ticker}  |  {pos.shares} shares  |  Avg cost ${pos.avg_cost:.2f}\n"
            f"Current price: ${price:.2f}  |  Stop: ${stop:.2f}\n"
            f"Unrealised P&L: {pnl_str} ({pnl_pct_str})\n"
            f"\n--- Situation Assessment ---\n"
            f"Stop:     {n['stop']}\n"
            f"P&L:      {n['pnl']}\n"
            f"Volume:   {n['volume']}\n"
            f"Trend:    {n['trend']}\n"
            f"CANSLIM:  {n['canslim']}\n"
            f"\n>>> {n['verdict']}\n"
            f"\n{self._canslim_text(canslim)}"
            f"\n\n{date.today().isoformat()}"
        )

    def _format_stop_loss_html(
        self, pos: Position, stock: StockRecord | None, n: dict[str, str]
    ) -> str:
        price = pos.current_price or 0.0
        stop = pos.stop_loss or 0.0
        pnl = pos.unrealised_pnl
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "--"
        pnl_pct = pos.unrealised_pnl_pct
        pnl_pct_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "--"
        pnl_color = "#c0392b" if (pnl or 0) < 0 else "#27ae60"

        verdict_bg = "#f8d7da" if "NOW" in n["verdict"] else "#fff3cd"
        verdict_border = "#e74c3c" if "NOW" in n["verdict"] else "#f39c12"

        rv = stock.rel_volume if stock else None
        vol_color = (
            "#e74c3c"
            if (rv or 0) >= 1.5
            else "#f39c12"
            if (rv or 0) >= 1.0
            else "#27ae60"
        )
        a = stock.analysis if stock else None
        sepa_count = sum(1 for v in (a.sepa_template or {}).values() if v) if a else 0
        trend_color = (
            "#e74c3c"
            if sepa_count <= 4
            else "#f39c12"
            if sepa_count <= 6
            else "#27ae60"
        )

        canslim = stock.analysis.canslim if stock and stock.analysis else None
        return email_templates.get_template("stop_loss.html").render(
            pos=pos,
            price_str=f"{price:.2f}",
            stop_str=f"{stop:.2f}",
            avg_cost_str=f"{pos.avg_cost:.2f}",
            pnl_str=pnl_str,
            pnl_pct_str=pnl_pct_str,
            pnl_color=pnl_color,
            vol_color=vol_color,
            trend_color=trend_color,
            verdict_bg=verdict_bg,
            verdict_border=verdict_border,
            n=n,
            canslim=canslim,
            today=date.today().isoformat(),
        )

    def check_portfolio_stops(
        self,
        positions: list[Position],
        stock_map: dict[str, StockRecord],
    ) -> int:
        """Check held positions for stop-loss hits and profit targets (#81).

        For each held critical event this sends an *immediate individual* email
        and queues the position into self._sell_alerts so it also appears in the
        run digest (the digest stays a complete record). A notification is
        recorded regardless of whether the email actually sent, so an
        unconfigured/failed SMTP still leaves an in-app record.

        The held-position path is authoritative for its ticker: a ticker that is
        also a watched row is de-duplicated in the digest (see
        ``send_summary_email``). Positions with NULL stop_loss/profit_target are
        skipped gracefully. Returns the count of held critical events.
        """
        self._sell_alerts.clear()
        for pos in positions:
            if pos.current_price is None:
                continue
            hit_stop = pos.stop_loss is not None and pos.current_price <= pos.stop_loss
            hit_target = (
                pos.profit_target_20 is not None
                and pos.current_price >= pos.profit_target_20
            )
            if not hit_stop and not hit_target:
                continue
            stock = stock_map.get(pos.ticker)
            self._sell_alerts.append((pos, stock))
            if hit_stop:
                self._alert_held_stop(pos, stock)
            else:
                self._alert_held_target(pos, stock)

        print(f"\n{len(self._sell_alerts)} held critical event(s) queued.")
        return len(self._sell_alerts)

    def _alert_held_stop(self, pos: Position, stock: StockRecord | None) -> None:
        """Send an immediate stop-loss email for a held position (#81).

        Records the notification unconditionally so the event survives an
        unconfigured or failed send.
        """
        price = pos.current_price or 0.0
        stop = pos.stop_loss or 0.0
        print(f"\nHELD STOP LOSS: {pos.ticker} @ ${price:.2f} (stop ${stop:.2f})")
        n = self._stop_loss_narrative(pos, stock)
        self.send_email(
            f"Stop Loss Hit: {pos.ticker}",
            self._format_stop_loss_html(pos, stock, n),
            self._format_stop_loss_text(pos, stock, n),
        )
        self._notify(
            "stop_loss_hit",
            f"Stop loss hit — {pos.ticker}",
            severity=NotificationSeverity.WARNING,
            body=f"{pos.ticker} @ ${price:.2f} breached its stop ${stop:.2f}.",
            ticker=pos.ticker,
        )

    def _alert_held_target(self, pos: Position, stock: StockRecord | None) -> None:
        """Send an immediate profit-target email for a held position (#81).

        Records the notification unconditionally so the event survives an
        unconfigured or failed send.
        """
        price = pos.current_price or 0.0
        target = pos.profit_target_20 or 0.0
        msg = (
            f"PROFIT TARGET REACHED: {pos.ticker} @ ${price:.2f} (target ${target:.2f})"
        )
        print(f"\n{msg}")
        self.send_email(f"Profit Target Reached: {pos.ticker}", f"<p>{msg}</p>", msg)
        self._notify(
            "profit_target",
            f"Profit target reached — {pos.ticker}",
            body=msg,
            ticker=pos.ticker,
        )

    def check_trailing_stops(
        self,
        positions: list[Position],
        trailing_stop_pct: float | None = None,
    ) -> int:
        """Detect large adverse moves from each held position's peak (#82).

        Maintains a persistent high-water-mark (the highest price seen since
        entry) per held position and, once per run, fires when a position has
        fallen ``trailing_stop_pct`` from that peak:
        ``current_price <= high_water_mark * (1 - trailing_stop_pct)``. Each
        trigger sends an immediate individual email, queues the position into
        ``self._sell_alerts`` so it also appears in the run digest, and records
        an in-app notification unconditionally (mirrors the #81 held path).

        ``trailing_stop_pct`` defaults to ``config.trailing_stop_pct()``. When
        it is unset/invalid the peak is still updated (so state stays warm) but
        no trigger is evaluated. The HWM is seeded from ``entry_price`` when
        present, else from the first observed ``current_price`` (SIPP-imported
        positions have ``entry_price = None``).

        Must run *after* ``check_portfolio_stops`` in the same run: a ticker
        already queued in ``self._sell_alerts`` (a hard stop-loss or profit
        target this run) is skipped here, so one ticker never yields two emails
        or two digest entries in a single run. Returns the trailing-trigger
        count.
        """
        pct = (
            trailing_stop_pct
            if trailing_stop_pct is not None
            else config.trailing_stop_pct()
        )
        valid_pct = pct is not None and 0 < pct < 1
        already_alerted = {pos.ticker for pos, _ in self._sell_alerts}
        triggered = 0
        for pos in positions:
            if pos.current_price is None:
                continue
            price = pos.current_price
            existing = self._position_state.get_hwm(pos.ticker)
            if existing is None and pos.entry_price is not None:
                self._position_state.record_high(pos.ticker, pos.entry_price)
            hwm = self._position_state.record_high(pos.ticker, price)
            if not valid_pct or pos.ticker in already_alerted:
                continue
            assert pct is not None  # narrowed by valid_pct
            if price <= hwm * (1 - pct):
                self._sell_alerts.append((pos, None))
                self._alert_trailing_stop(pos, hwm, pct)
                already_alerted.add(pos.ticker)
                triggered += 1
        print(f"\n{triggered} trailing-stop trigger(s) queued.")
        return triggered

    def _alert_trailing_stop(
        self,
        pos: Position,
        hwm: float,
        pct: float,
    ) -> None:
        """Send an immediate trailing-stop email for a held position (#82).

        Records the notification unconditionally so the event survives an
        unconfigured or failed send.
        """
        price = pos.current_price or 0.0
        drop_pct = (hwm - price) / hwm * 100 if hwm else 0.0
        msg = (
            f"TRAILING STOP: {pos.ticker} @ ${price:.2f} is "
            f"{drop_pct:.1f}% below its peak ${hwm:.2f} "
            f"(threshold {pct * 100:.0f}%)"
        )
        print(f"\n{msg}")
        self.send_email(
            f"Trailing Stop: {pos.ticker}",
            f"<p>{msg}</p>",
            msg,
        )
        self._notify(
            "trailing_stop",
            f"Trailing stop — {pos.ticker}",
            severity=NotificationSeverity.WARNING,
            body=msg,
            ticker=pos.ticker,
        )

    @staticmethod
    def _currency_symbol(currency: str) -> str:
        """Return the display symbol for a position's price currency."""
        return "$" if currency == "USD" else "£"

    def send_summary_email(
        self,
        positions: list[Position] | None = None,
        gbp_totals: tuple[float, float, float] | None = None,
        market_narrative: MarketNarrative | None = None,
    ) -> None:
        """Send one consolidated daily summary email.

        Always sends: starts with a portfolio snapshot, then SELL alerts (top),
        then BUY alerts. positions is optional — used for the snapshot section.
        gbp_totals is an optional (total_value_gbp, total_cost_gbp,
        total_pnl_gbp), pre-converted across currencies (see
        ``PortfolioService.gbp_totals``) so the aggregate figures match the
        web UI's summary cards. When omitted, totals fall back to a naive
        same-currency sum of ``positions`` (e.g. for callers/tests with an
        all-GBP portfolio).

        ``market_narrative``, when given, is rendered at the very top of the
        digest (before the portfolio snapshot). It is a typed, provider-
        agnostic object — Phase 1 passes a deterministic sector-allocation +
        market-cycle blurb (see ``app.agents.scanner.market_narrative``); a
        later phase can pass a Claude-generated one instead without this
        method changing.
        """
        today = date.today().isoformat()
        # A held critical event (hard stop / trailing / target) is authoritative
        # for its ticker, so a watched-list row duplicating it is dropped.
        critical_tickers = {pos.ticker for pos, _ in self._sell_alerts}
        # #113: a stop/sell signal is only actionable for a stock you actually
        # hold — a watched-list setup breaking its stop when it isn't owned is
        # noise. Gate watched stops on portfolio membership so only held names
        # produce a SELL line. check_positions still marks the row "stopped" in
        # the DB, so watchlist bookkeeping is untouched — only the alert is
        # suppressed. Buy/breakout entries are not gated. positions is optional;
        # when absent the held set is empty, so no watched-stop lines are shown
        # (genuine held sells still flow via the authoritative _sell_alerts path).
        held_tickers = {pos.ticker for pos in positions} if positions else set()
        watched_stops = [
            row
            for row in self._watched_stops
            if row[0] in held_tickers and row[0] not in critical_tickers
        ]
        entry_triggered = [
            row for row in self._entry_triggered if row[0] not in critical_tickers
        ]
        sell_count = len(self._sell_alerts)
        buy_count = len(self._buy_alerts)
        watched_count = len(entry_triggered) + len(watched_stops)

        if sell_count or buy_count:
            subject = f"Stock Alerts {today} — " + (
                f"{sell_count} SELL" + (f", {buy_count} BUY" if buy_count else "")
                if sell_count
                else f"{buy_count} BUY"
            )
        elif watched_count:
            subject = f"Stock Alerts {today} — {watched_count} watchlist signal(s)"
        else:
            subject = f"Stock Scanner {today} — No alerts"

        # ── Market narrative (text) ─────────────────────────────────────────
        text_parts: list[str] = [f"Stock Scanner Summary — {today}\n{'=' * 50}"]
        if market_narrative:
            narrative_lines = [f"\n\nMARKET NARRATIVE\n{market_narrative.headline}"]
            narrative_lines.extend(f"  - {b}" for b in market_narrative.bullets)
            narrative_lines.append(f"  ({market_narrative.not_advice})")
            if market_narrative.sources:
                sources_str = ", ".join(
                    f"{s.label} ({s.url})" if s.url else s.label
                    for s in market_narrative.sources
                )
                narrative_lines.append(f"  Sources: {sources_str}")
            text_parts.append("\n".join(narrative_lines))

        # ── Portfolio snapshot (text) ───────────────────────────────────────
        if positions:
            if gbp_totals is not None:
                total_value, total_cost, total_pnl = gbp_totals
            else:
                total_value = sum(p.current_value for p in positions if p.current_value)
                total_cost = sum(p.total_cost for p in positions)
                total_pnl = total_value - total_cost
            pnl_pct = total_pnl / total_cost * 100 if total_cost else 0.0
            text_parts.append(
                f"\n\nPORTFOLIO SNAPSHOT\n"
                f"  Positions : {len(positions)}\n"
                f"  Mkt Value : £{total_value:,.2f}\n"
                f"  Cost Basis: £{total_cost:,.2f}\n"
                f"  P&L       : £{total_pnl:+,.2f} ({pnl_pct:+.1f}%)\n"
            )
            for p in positions:
                sym = self._currency_symbol(p.price_currency)
                pnl_str = (
                    f"{sym}{p.unrealised_pnl:+.2f} ({p.unrealised_pnl_pct:+.1f}%)"
                    if p.unrealised_pnl is not None
                    else "--"
                )
                text_parts.append(
                    f"  {p.ticker:<6} {p.shares:>8.1f} shares"
                    f"  price {sym}{p.current_price or 0:.2f}  P&L {pnl_str}"
                )

        def _conviction_rank(item: tuple[StockRecord, str]) -> int:
            verdict = self._breakout_narrative(item[0])["verdict"]
            if "HIGH CONVICTION" in verdict:
                return 0
            if "MODERATE CONVICTION" in verdict:
                return 1
            return 2

        strong_buys = sorted(
            [
                (s, t)
                for s, t in self._buy_alerts
                if "LOW CONVICTION" not in self._breakout_narrative(s)["verdict"]
            ],
            key=_conviction_rank,
        )
        # Split the surfaced setups by urgency: a genuine breakout (a
        # ``classify_alert`` trigger) is happening now; a "Stay Alert" name is
        # only approaching its pivot. They used to share one BUY/BREAKOUT
        # section, which buried the act-now rows (#162).
        breaking_out = [(s, t) for s, t in strong_buys if classify_alert(s).trigger]
        approaching = [(s, t) for s, t in strong_buys if not classify_alert(s).trigger]

        # Whether there's any alert content this run — same condition that
        # previously gated the "How to read" legend (#163); now decides
        # whether the CTA lists section guidance or shows the neutral
        # fallback.
        cta_active = bool(self._sell_alerts or watched_count or strong_buys)
        if cta_active:
            cta_lines = ["\n\nHOW TO USE THIS EMAIL"]
            for icon, heading, _color, desc in _CTA_GROUPS:
                cta_lines.append(f"  {icon} {heading.upper()}: {desc}")
            cta_lines.append(f"  {_CTA_NOT_ADVICE}")
            text_parts.append("\n".join(cta_lines))
        else:
            text_parts.append(f"\n\n{_CTA_NO_ALERTS}\n{_CTA_NOT_ADVICE}")

        if self._sell_alerts:
            text_parts.append("\n\n*** SELL / STOP LOSS (held) ***\n")
            for pos, stock in self._sell_alerts:
                n = self._stop_loss_narrative(pos, stock)
                text_parts.append(self._format_stop_loss_text(pos, stock, n))
                text_parts.append("\n" + "-" * 40)

        if watched_stops:
            text_parts.append("\n\n*** TRACKED SETUP — STOPPED OUT ***\n")
            for ticker, current, stop, wstock in watched_stops:
                canslim = (
                    wstock.analysis.canslim if wstock and wstock.analysis else None
                )
                text_parts.append(
                    f"  {ticker}: ${current:.2f} hit stop ${stop:.2f}\n"
                    f"  {self._canslim_text(canslim)}"
                )

        if entry_triggered or breaking_out:
            text_parts.append("\n\n*** BUY ***\n")
            for ticker, current, entry, wstock in entry_triggered:
                header = (
                    f"  ENTRY TRIGGERED — {ticker}: "
                    f"${current:.2f} crossed entry ${entry:.2f}"
                )
                if wstock and wstock.analysis:
                    text_parts.append(header + "\n" + self.format_alert_text(wstock))
                else:
                    text_parts.append(f"{header}\n  {_ANALYSIS_UNAVAILABLE}")
                text_parts.append("\n" + "-" * 40)
            for stock, trigger in breaking_out:
                text_parts.append(self.format_alert_text(stock, trigger))
                text_parts.append("\n" + "-" * 40)

        if approaching:
            text_parts.append(
                "\n\n*** APPROACHING ENTRY (watch — not yet at pivot) ***\n"
            )
            for stock, trigger in approaching:
                text_parts.append(self.format_alert_text(stock, trigger))
                text_parts.append("\n" + "-" * 40)

        if not self._sell_alerts and not self._buy_alerts and not watched_count:
            text_parts.append("\n\nNo actionable alerts this run.")

        text_body = "\n".join(text_parts)

        # ── Portfolio snapshot (view model) ─────────────────────────────────
        snapshot: dict[str, Any] | None = None
        if positions:
            if gbp_totals is not None:
                total_value, total_cost, total_pnl = gbp_totals
            else:
                total_value = sum(p.current_value for p in positions if p.current_value)
                total_cost = sum(p.total_cost for p in positions)
                total_pnl = total_value - total_cost
            pnl_pct = total_pnl / total_cost * 100 if total_cost else 0.0
            # Blue for a portfolio gain, not the BUY-signal green (#168 review
            # feedback) — "my portfolio is up" and "this is a buy signal"
            # shouldn't share a colour.
            pnl_color = "#2980b9" if total_pnl >= 0 else "#c0392b"
            snapshot = {
                "rows": [
                    {
                        "ticker": p.ticker,
                        "shares": f"{p.shares:g}",
                        "price": (
                            f"{self._currency_symbol(p.price_currency)}"
                            f"{p.current_price or 0:.2f}"
                        ),
                        "value": (
                            f"{self._currency_symbol(p.price_currency)}"
                            f"{p.current_value or 0:,.2f}"
                        ),
                        "pnl_pct": f"{p.unrealised_pnl_pct:+.1f}%",
                        "pnl_color": (
                            "#2980b9" if (p.unrealised_pnl or 0) >= 0 else "#c0392b"
                        ),
                    }
                    for p in positions
                    if p.current_value
                ],
                "total_value": f"{total_value:,.2f}",
                "total_cost": f"{total_cost:,.2f}",
                "total_pnl": f"{total_pnl:+,.2f}",
                "pnl_pct": f"{pnl_pct:+.1f}%",
                "pnl_color": pnl_color,
            }

        # ── HTML body ──────────────────────────────────────────────────────
        sell_cards = [
            self._sell_card_context(pos, stock, self._stop_loss_narrative(pos, stock))
            for pos, stock in self._sell_alerts
        ]
        watched_stop_rows = [
            self._watched_row_ctx(t, c, lvl, stock=wstock, stop=True)
            for t, c, lvl, wstock in watched_stops
        ]
        entry_triggered_cards = [
            self._buy_card_context(wstock, "ENTRY TRIGGERED", entry_override=lvl)
            for _t, _c, lvl, wstock in entry_triggered
            if wstock is not None
        ]
        breaking_out_cards = [
            self._buy_card_context(stock, trigger) for stock, trigger in breaking_out
        ]
        approaching_cards = [
            self._buy_card_context(stock, trigger) for stock, trigger in approaching
        ]
        low_conviction_count = len(self._buy_alerts) - len(strong_buys)
        any_alerts = bool(self._sell_alerts or strong_buys or watched_count)

        html_body = email_templates.get_template("summary.html").render(
            today=today,
            narrative=market_narrative,
            snapshot=snapshot,
            cta_active=cta_active,
            cta_groups=_CTA_GROUPS,
            cta_not_advice=_CTA_NOT_ADVICE,
            cta_no_alerts=_CTA_NO_ALERTS,
            sell_cards=sell_cards,
            watched_stop_rows=watched_stop_rows,
            entry_triggered_cards=entry_triggered_cards,
            breaking_out_cards=breaking_out_cards,
            approaching_cards=approaching_cards,
            low_conviction_count=low_conviction_count,
            any_alerts=any_alerts,
        )

        if not self.send_email(subject, html_body, text_body):
            return
        # Digest actually went out: mirror the watched-setup signals it carried
        # into the notification centre (#80/#81). Held critical events are
        # already recorded unconditionally in check_portfolio_stops, so they are
        # not re-notified here.
        for ticker, current, entry, _wstock in entry_triggered:
            self._notify(
                "entry_triggered",
                f"Entry triggered — {ticker}",
                body=f"{ticker} @ ${current:.2f} crossed entry ${entry:.2f}.",
                ticker=ticker,
            )
        for ticker, current, stop, _wstock in watched_stops:
            self._notify(
                "stop_loss_hit",
                f"Stop loss hit — {ticker}",
                severity=NotificationSeverity.WARNING,
                body=f"{ticker} @ ${current:.2f} hit stop ${stop:.2f}.",
                ticker=ticker,
            )
        for stock, trigger in strong_buys:
            self._notify(
                "breakout",
                f"{trigger} — {stock.ticker}",
                body=f"{stock.ticker} qualified as a buy ({trigger}).",
                ticker=stock.ticker,
            )

    @staticmethod
    def _yahoo_url(ticker: str) -> str:
        return f"https://finance.yahoo.com/quote/{ticker.replace('.', '-')}"

    def _watched_row_ctx(
        self,
        ticker: str,
        current: float,
        level: float,
        *,
        stock: StockRecord | None,
        stop: bool,
    ) -> dict[str, Any]:
        """Render context for one watched-setup entry/stop signal row (#81).

        ``stock`` is the current run's analysed StockRecord (#312) — its
        CANSLIM score renders when available, else the tile shows the
        explicit unavailable state.
        """
        canslim = stock.analysis.canslim if stock and stock.analysis else None
        return {
            "ticker": ticker,
            "current_str": f"{current:.2f}",
            "level_str": f"{level:.2f}",
            "url": self._yahoo_url(ticker),
            "canslim": canslim,
            "stop": stop,
        }

    def _sell_card_context(
        self, pos: Position, stock: StockRecord | None, n: dict[str, str]
    ) -> dict[str, Any]:
        """Render context shared by the digest's sell cards."""
        price = pos.current_price or 0.0
        stop = pos.stop_loss or 0.0
        pnl = pos.unrealised_pnl
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "--"
        pnl_pct = pos.unrealised_pnl_pct
        pnl_pct_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "--"
        pnl_color = "#c0392b" if (pnl or 0) < 0 else "#27ae60"
        verdict_bg = "#f8d7da" if "NOW" in n["verdict"] else "#fff3cd"
        verdict_border = "#e74c3c" if "NOW" in n["verdict"] else "#f39c12"
        canslim = stock.analysis.canslim if stock and stock.analysis else None
        return {
            "pos": pos,
            "url": self._yahoo_url(pos.ticker),
            "stop_str": f"{stop:.2f}",
            "price_str": f"{price:.2f}",
            "pnl_str": pnl_str,
            "pnl_pct_str": pnl_pct_str,
            "pnl_color": pnl_color,
            "verdict_bg": verdict_bg,
            "verdict_border": verdict_border,
            "n": n,
            "canslim": canslim,
        }

    def _buy_card_context(
        self,
        stock: StockRecord,
        trigger: str,
        *,
        entry_override: float | None = None,
    ) -> dict[str, Any]:
        """Render context shared by ``_buy_card_html`` and the digest.

        Every Buy-section ticker renders through this one contract so the
        emailed BUY and APPROACHING ENTRY cards expose identical fields,
        labels, ordering, and value formatting (#356). ``entry_override``
        supplies the pivot for a watchlist setup whose ``StockRecord`` carries
        no full analysis; the analysis-derived fields then render as an
        explicit unavailable state rather than being dropped.
        """
        a = stock.analysis
        score_color = (
            "#27ae60"
            if a and a.score >= 8
            else "#f39c12"
            if a and a.score >= 6
            else "#e74c3c"
        )
        score_str = f"Score {a.score}/10" if a else "Score —"
        stage_str = a.stage if a else "—"
        entry_price = a.entry_price if a and a.entry_price else entry_override
        entry = f"${entry_price:.2f}" if entry_price else "—"
        stop = f"${a.stop_loss:.2f}" if a and a.stop_loss else "—"
        if entry_price and a and a.stop_loss:
            risk_pct = (entry_price - a.stop_loss) / entry_price * 100
            risk_str = f"{risk_pct:.1f}%"
        else:
            risk_str = "—"
        if a and a.score >= 8 and stock.rel_volume >= 1.5:
            verdict_bg, verdict_border = "#d4edda", "#27ae60"
        elif a and a.score >= 7:
            verdict_bg, verdict_border = "#fff3cd", "#f39c12"
        elif a:
            verdict_bg, verdict_border = "#f8d7da", "#e74c3c"
        else:
            verdict_bg, verdict_border = "#fff3cd", "#f39c12"
        congress = self._congress_summary(stock)
        congress_net_color = "#555"
        if congress:
            net = (stock.congress_buys or 0) - (stock.congress_sells or 0)
            congress_net_color = (
                "#27ae60" if net > 0 else "#c0392b" if net < 0 else "#555"
            )
        return {
            "stock": stock,
            "trigger": trigger,
            "url": self._yahoo_url(stock.ticker),
            "score_color": score_color,
            "score_str": score_str,
            "stage_str": stage_str,
            "entry": entry,
            "stop": stop,
            "risk_str": risk_str,
            "n": (
                self._breakout_narrative(stock)
                if a
                else dict(_BLANK_BREAKOUT_NARRATIVE)
            ),
            "verdict_bg": verdict_bg,
            "verdict_border": verdict_border,
            "congress": congress,
            "congress_net_color": congress_net_color,
            "svg_chart": self._svg_chart(stock.price_history, score_color),
            "canslim": a.canslim if a else None,
        }

    def _buy_card_html(self, stock: StockRecord, trigger: str) -> str:
        """Compact HTML card for one breakout alert."""
        ctx = self._buy_card_context(stock, trigger)
        return get_macro("_buy_card.html", "buy_card")(**ctx)

    def send_email(self, subject: str, html_body: str, text_body: str) -> bool:
        """Send one email; return True only if it was actually dispatched.

        Returns False when SMTP is unconfigured or the send raises, so callers
        can record "alerts sent" notifications (#80) that mirror reality.
        """
        if (
            not self.email_config.user
            or not self.email_config.password
            or not self.email_config.recipient
        ):
            print("  [email] (not configured)")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.email_config.user
        msg["To"] = self.email_config.recipient
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(self.email_config.host, self.email_config.port) as server:
                server.starttls()
                server.login(self.email_config.user, self.email_config.password)
                server.sendmail(
                    self.email_config.user,
                    self.email_config.recipient,
                    msg.as_string(),
                )
            print(f"  [email] sent to {self.email_config.recipient}")
            return True
        except Exception as error:
            print(f"  [email] error: {error}")
            return False

    @staticmethod
    def _recommendation_text_body(
        result: RecommendationResultV1,
        portfolio_name: str,
        strategy_display_name: str,
        today: str,
    ) -> str:
        """Build the plain-text equivalent of the recommendation email (#442).

        Sections mirror the HTML template's fixed order: freshness warning →
        Sell → Hold → Buy → provenance footer.
        """
        lines = [
            f"Portfolio Recommendations — {portfolio_name}",
            f"Strategy: {strategy_display_name} · {today}",
            "",
        ]
        if result.freshness != "fresh":
            lines.extend(
                [
                    f"WARNING: Scan data is {result.freshness} — recommendations "
                    "use the last published analysis.",
                    "",
                ]
            )
        coverage = result.coverage
        if not coverage.complete:
            lines.extend(_recommendation_coverage_lines(coverage))
        unavailable = (
            "  Signals unavailable — the evidence this Strategy needs was incomplete."
        )
        for label in ("SELL", "HOLD", "BUY"):
            action = label.lower()
            rows = [r for r in result.recommendations if r.action == action]
            lines.append(f"{label} ({len(rows)})")
            if not rows:
                # A degraded path is still invoked, so only an
                # incompatible path replaces the honest wording (#471).
                supported = (
                    coverage.exit_supported
                    if action == "sell"
                    else coverage.entry_supported
                    if action == "buy"
                    else True
                )
                lines.append(f"  No {action} signals." if supported else unavailable)
            for rec in rows:
                lines.append(f"  {rec.ticker}: {rec.reason} [{rec.rule_id}]")
                for warning in rec.evidence_warnings:
                    lines.append(f"    warning: {warning}")
            lines.append("")
        lines.append(
            f"Strategy {result.strategy_id} · "
            f"digest {result.strategy_source_digest[:8]} · "
            f"run {result.analysis_run_id}"
        )
        lines.append("No trades were placed — this is analysis only.")
        return "\n".join(lines)

    def send_portfolio_recommendation_email(
        self,
        result: RecommendationResultV1,
        portfolio_name: str,
        strategy_display_name: str,
        market_narrative: MarketNarrative | None = None,
        portfolio_summary: dict[str, str] | None = None,
    ) -> bool:
        """Send one portfolio's Strategy recommendation email (#442).

        Renders the already-evaluated ``RecommendationResultV1`` — action
        rules are never recalculated here — and dispatches via the shared
        ``send_email`` transport, returning its bool. No
        ``_buy_alerts``/``_sell_alerts`` mutation.
        """
        sells = [r for r in result.recommendations if r.action == "sell"]
        holds = [r for r in result.recommendations if r.action == "hold"]
        buys = [r for r in result.recommendations if r.action == "buy"]
        # Date the email by the run that produced it, not the wall clock —
        # a delayed retry of an earlier run must not misdate the subject.
        today = result.generated_at.date().isoformat()
        # Portfolio/strategy names are user-controlled; strip any embedded
        # newline before they reach the SMTP Subject header (header injection).
        clean_portfolio = " ".join(portfolio_name.splitlines())
        clean_strategy = " ".join(strategy_display_name.splitlines())
        subject = f"Recommendations — {clean_portfolio} · {clean_strategy} · {today}"
        html_body = email_templates.get_template("recommendations.html").render(
            portfolio_name=portfolio_name,
            strategy_name=strategy_display_name,
            today=today,
            freshness=result.freshness,
            market_narrative=market_narrative,
            portfolio_summary=portfolio_summary,
            sells=sells,
            holds=holds,
            buys=buys,
            strategy_id=result.strategy_id,
            digest=result.strategy_source_digest,
            run_id=result.analysis_run_id,
            parameters=result.parameters,
            coverage=result.coverage,
        )
        text_body = self._recommendation_text_body(
            result, portfolio_name, strategy_display_name, today
        )
        return self.send_email(subject, html_body, text_body)


if __name__ == "__main__":
    analysis_file = ANALYSIS_JSON
    agent = AlertAgent()
    with open(analysis_file, encoding="utf-8") as handle:
        payload = [StockRecord.model_validate(item) for item in json.load(handle)]
    agent.run(payload)
