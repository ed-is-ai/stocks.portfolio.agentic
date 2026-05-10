"""
Alert Agent — sends email notifications when a stock scores above threshold.
Reads analysis results and fires alerts for near-entry setups.
"""

import json
import os
import smtplib
import sqlite3
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

from pydantic import PrivateAttr

from ms_agent_framework import Agent
from models import EmailConfig, Position, StockRecord


ALERT_COOLDOWN_HOURS = 24
DB_PATH = str(Path(__file__).parent / "alerts.db")


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


class AlertAgent(Agent):
    name: str = "AlertAgent"
    db_path: str = DB_PATH
    email_config: EmailConfig = EMAIL_CONFIG

    _buy_alerts: list[tuple[StockRecord, str]] = PrivateAttr(default_factory=list)
    _sell_alerts: list[tuple[Position, StockRecord | None]] = PrivateAttr(default_factory=list)

    def check_positions(
        self, stocks: list[StockRecord], conn: sqlite3.Connection
    ) -> None:
        """Fire follow-up alerts when a watching position crosses entry or stop loss."""
        rows = conn.execute(
            "SELECT rowid, ticker, entry_price, stop_loss FROM alerts WHERE status='watching'"
        ).fetchall()
        if not rows:
            return
        price_map = {s.ticker: s.price for s in stocks}
        for rowid, ticker, entry_price, stop_loss in rows:
            current = price_map.get(ticker)
            if current is None:
                continue
            if entry_price is not None and current >= entry_price:
                msg = (
                    f"ENTRY TRIGGERED: {ticker} @ ${current:.2f} "
                    f"(entry was ${entry_price:.2f})"
                )
                print(f"\n{msg}")
                self.send_email(f"Entry Triggered: {ticker}", f"<p>{msg}</p>", msg)
                conn.execute(
                    "UPDATE alerts SET status='entered' WHERE rowid=?", (rowid,)
                )
                conn.commit()
            elif stop_loss is not None and current <= stop_loss:
                msg = (
                    f"STOP LOSS HIT: {ticker} @ ${current:.2f} "
                    f"(stop was ${stop_loss:.2f})"
                )
                print(f"\n{msg}")
                self.send_email(f"Stop Loss Hit: {ticker}", f"<p>{msg}</p>", msg)
                conn.execute(
                    "UPDATE alerts SET status='stopped' WHERE rowid=?", (rowid,)
                )
                conn.commit()

    def run(self, payload: Iterable[StockRecord]) -> int:
        results = list(payload)
        conn = self.init_db()
        conn.execute("DELETE FROM alerts")
        conn.commit()
        self.check_positions(results, conn)
        self._buy_alerts.clear()

        for stock in results:
            if not self.should_alert(stock, conn):
                continue
            trigger = self.alert_trigger(stock)
            assert trigger is not None
            print(f"\nALERT: {stock.ticker} [{trigger}]")
            self._buy_alerts.append((stock, trigger))
            self.record_alert(conn, stock)

        conn.close()
        print(f"\n{len(self._buy_alerts)} buy alert(s) queued.")
        return len(self._buy_alerts)

    def init_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                ticker TEXT NOT NULL,
                alerted_at TEXT NOT NULL,
                score INTEGER,
                stage TEXT,
                summary TEXT
            )
            """
        )
        for col_sql in [
            "ALTER TABLE alerts ADD COLUMN entry_price REAL",
            "ALTER TABLE alerts ADD COLUMN stop_loss REAL",
            "ALTER TABLE alerts ADD COLUMN status TEXT DEFAULT 'watching'",
        ]:
            try:
                conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        return conn

    def was_recently_alerted(self, conn: sqlite3.Connection, ticker: str) -> bool:
        watching = conn.execute(
            "SELECT 1 FROM alerts WHERE ticker=? AND status='watching' LIMIT 1",
            (ticker,),
        ).fetchone()
        if watching:
            return True
        row = conn.execute(
            "SELECT alerted_at FROM alerts WHERE ticker=? ORDER BY alerted_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return False
        last = row[0][:13]
        diff = datetime.now(timezone.utc) - datetime.fromisoformat(last + ":00:00+00:00")
        return diff.total_seconds() < ALERT_COOLDOWN_HOURS * 3600

    def record_alert(self, conn: sqlite3.Connection, stock: StockRecord) -> None:
        conn.execute(
            """INSERT INTO alerts
               (ticker, alerted_at, score, stage, summary, entry_price, stop_loss, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'watching')""",
            (
                stock.ticker,
                datetime.now(timezone.utc).isoformat(),
                stock.analysis.score,
                stock.analysis.stage,
                stock.analysis.summary,
                stock.analysis.entry_price,
                stock.analysis.stop_loss,
            ),
        )
        conn.commit()

    def alert_trigger(self, stock: StockRecord) -> str | None:
        """Return a trigger label if this stock warrants an immediate alert, else None.

        Only fires for genuinely actionable breakout events:
          - fresh_breakout: VCP entry_zone just transitioned to broken_out this run
          - multiyear_breakout: price cleared a significant multi-year base pivot
        """
        if not stock.analysis:
            return None
        a = stock.analysis
        is_fresh = a.fresh_breakout
        is_myb = a.multiyear_breakout
        if is_fresh and is_myb:
            return "VCP + Multi-Year Base Breakout"
        if is_fresh:
            return "VCP Breakout"
        if is_myb:
            pivot = f"${a.multiyear_pivot:.2f}" if a.multiyear_pivot else ""
            return f"Multi-Year Base Breakout {pivot}"
        return None

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
            vol_line = f"Strong volume confirmation ({rv:.1f}x average) — institutions buying."
        elif rv >= 1.0:
            vol_line = f"Moderate volume ({rv:.1f}x average) — acceptable but watch for follow-through."
        else:
            vol_line = (
                f"Weak volume ({rv:.1f}x average) — breakout lacks conviction; wait for volume surge."
            )

        # --- SEPA ---
        sepa_count = 0
        sepa_detail = ""
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
                sepa_line = "Perfect SEPA trend template (8/8) — textbook Stage 2 uptrend."
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
            canslim_line = (
                f"CANSLIM {cs.total}/14: {'; '.join(parts)}; {m_str}."
            )
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
            mom_line = f"Momentum {mo.total}/14: {'; '.join(mom_parts)}." if mom_parts else f"Momentum {mo.total}/14."
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

    def should_alert(self, stock: StockRecord, conn: sqlite3.Connection) -> bool:
        if self.alert_trigger(stock) is None:
            return False
        if self.was_recently_alerted(conn, stock.ticker):
            print(f"  [skip] {stock.ticker}: already alerted recently")
            return False
        return True

    def format_alert_text(self, stock: StockRecord, trigger: str | None = None) -> str:
        analysis = stock.analysis
        assert analysis is not None
        strengths = "\n".join(f"  + {item}" for item in analysis.strengths)
        risks = "\n".join(f"  - {item}" for item in analysis.risks)

        entry = f"${analysis.entry_price:.2f}" if analysis.entry_price else "—"
        stop = f"${analysis.stop_loss:.2f}" if analysis.stop_loss else "—"
        if analysis.entry_price and analysis.stop_loss:
            risk_pct = ((analysis.entry_price - analysis.stop_loss) / analysis.entry_price) * 100
            risk_str = f"{risk_pct:.1f}% below entry"
        else:
            risk_str = "—"

        canslim_str = ""
        if analysis.canslim:
            cs = analysis.canslim
            canslim_str = (
                f"\nCANSLIM {cs.total}/14: "
                f"C={cs.C} A={cs.A} N={cs.N} S={cs.S} L={cs.L} I={cs.I} M={cs.M}"
            )

        n = self._breakout_narrative(stock)
        trigger_line = f"** {trigger} **\n\n" if trigger else ""
        body = (
            f"{trigger_line}{stock.ticker} — Score {analysis.score}/10 | {analysis.stage}\n"
            f"Price: ${stock.price}  |  RSI: {stock.rsi14}  |  RelVol: {stock.rel_volume}x\n"
            f"Distance from 52w high: {stock.pct_from_52w_high}%\n"
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
        strengths_html = "".join(f"<li>&#10003; {item}</li>" for item in analysis.strengths)
        risks_html = "".join(f"<li>&#9888; {item}</li>" for item in analysis.risks)

        if analysis.score >= 8:
            score_color = "#27ae60"
        elif analysis.score >= 6:
            score_color = "#f39c12"
        else:
            score_color = "#e74c3c"
        risks_section = (
            f"<ul style='padding-left:20px;color:#c0392b'>{risks_html}</ul>" if risks_html else ""
        )

        entry = f"${analysis.entry_price:.2f}" if analysis.entry_price else "—"
        stop = f"${analysis.stop_loss:.2f}" if analysis.stop_loss else "—"
        if analysis.entry_price and analysis.stop_loss:
            risk_pct = ((analysis.entry_price - analysis.stop_loss) / analysis.entry_price) * 100
            risk_str = f"{risk_pct:.1f}%"
        else:
            risk_str = "—"

        canslim_section = ""
        if analysis.canslim:
            cs = analysis.canslim
            def _bar(val: int) -> str:
                return "&#9632;" * val + "&#9633;" * (2 - val)
            canslim_section = f"""
          <table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:0.85em">
            <tr><td colspan="3" style="padding:4px 6px;background:#eee;font-weight:bold">
              CANSLIM Score: {cs.total}/14
            </td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8;width:30%">C &mdash; Momentum</td>
                <td style="padding:3px 6px">{_bar(cs.C)}</td><td style="padding:3px 6px">{cs.C}/2</td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8">A &mdash; Annual Strength</td>
                <td style="padding:3px 6px">{_bar(cs.A)}</td><td style="padding:3px 6px">{cs.A}/2</td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8">N &mdash; Near New High</td>
                <td style="padding:3px 6px">{_bar(cs.N)}</td><td style="padding:3px 6px">{cs.N}/2</td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8">S &mdash; Volume</td>
                <td style="padding:3px 6px">{_bar(cs.S)}</td><td style="padding:3px 6px">{cs.S}/2</td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8">L &mdash; Leader (RSI)</td>
                <td style="padding:3px 6px">{_bar(cs.L)}</td><td style="padding:3px 6px">{cs.L}/2</td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8">I &mdash; Stage Structure</td>
                <td style="padding:3px 6px">{_bar(cs.I)}</td><td style="padding:3px 6px">{cs.I}/2</td></tr>
            <tr><td style="padding:3px 6px;background:#f8f8f8">M &mdash; SMA Alignment</td>
                <td style="padding:3px 6px">{_bar(cs.M)}</td><td style="padding:3px 6px">{cs.M}/2</td></tr>
          </table>"""

        trigger_banner = ""
        if trigger:
            trigger_banner = (
                f"<div style=\"background:#1e3a5f;color:white;padding:10px 16px;"
                f"border-radius:4px;font-weight:bold;margin-bottom:16px;font-size:0.95em\">"
                f"&#9889; {trigger}</div>"
            )
        return f"""
        <html><body style=\"font-family:Arial,sans-serif;max-width:560px;margin:auto;padding:20px\">
          {trigger_banner}
          <h2 style=\"border-bottom:2px solid {score_color};padding-bottom:8px\">
            {stock.ticker}
            <span style=\"color:{score_color};font-size:0.9em\">
              &#9679; Score {analysis.score}/10 &mdash; {analysis.stage}
            </span>
          </h2>
          <table style=\"width:100%;border-collapse:collapse;margin-bottom:16px\">
            <tr>
              <td style=\"padding:6px;background:#f8f8f8\"><b>Price</b></td>
              <td style=\"padding:6px\">${stock.price}</td>
              <td style=\"padding:6px;background:#f8f8f8\"><b>RSI</b></td>
              <td style=\"padding:6px\">{stock.rsi14}</td>
            </tr>
            <tr>
              <td style=\"padding:6px;background:#f8f8f8\"><b>Rel. Volume</b></td>
              <td style=\"padding:6px\">{stock.rel_volume}x</td>
              <td style=\"padding:6px;background:#f8f8f8\"><b>From 52w High</b></td>
              <td style=\"padding:6px\">{stock.pct_from_52w_high}%</td>
            </tr>
            <tr>
              <td style=\"padding:6px;background:#dff0d8\"><b>Entry</b></td>
              <td style=\"padding:6px;color:#27ae60;font-weight:bold\">{entry}</td>
              <td style=\"padding:6px;background:#f2dede\"><b>Stop Loss</b></td>
              <td style=\"padding:6px;color:#c0392b;font-weight:bold\">{stop}</td>
            </tr>
            <tr>
              <td style=\"padding:6px;background:#f8f8f8\" colspan="2"><b>Risk to Stop</b></td>
              <td style=\"padding:6px\" colspan="2">{risk_str} below entry</td>
            </tr>
          </table>
          <p style=\"font-style:italic;color:#555\">{analysis.summary}</p>
          {self._narrative_html(stock)}
          <ul style=\"padding-left:20px;color:#27ae60\">{strengths_html}</ul>
          {risks_section}
          {canslim_section}
          {self._svg_chart(stock.price_history, score_color)}
          <p style=\"color:#aaa;font-size:0.8em;margin-top:24px\">{date.today().isoformat()} &mdash; Momentum Scanner</p>
        </body></html>
        """

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
        area_pts = (
            f"{x(0):.1f},{h - pad} "
            + points
            + f" {x(n - 1):.1f},{h - pad}"
        )

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'style="display:block;margin:16px 0;font-family:Arial,sans-serif">'
            f'<rect width="{w}" height="{h}" fill="#f8f8f8" rx="4"/>'
            f'<polygon points="{area_pts}" fill="{line_color}" opacity="0.12"/>'
            f'<polyline points="{points}" fill="none" stroke="{line_color}" '
            f'stroke-width="2" stroke-linejoin="round"/>'
            f'<circle cx="{x(n-1):.1f}" cy="{y(prices[-1]):.1f}" r="3" fill="{line_color}"/>'
            f'<text x="{x(0)}" y="{h - 1}" font-size="9" fill="#aaa">52w ago</text>'
            f'<text x="{x(n-1) - 20}" y="{h - 1}" font-size="9" fill="#aaa">now</text>'
            f'<text x="{pad + 2}" y="{y(hi) + 9}" font-size="9" fill="#888">'
            f'${hi:,.2f}</text>'
            f'<text x="{pad + 2}" y="{y(lo) - 3}" font-size="9" fill="#888">'
            f'${lo:,.2f}</text>'
            f"</svg>"
        )

    def _narrative_html(self, stock: StockRecord) -> str:
        """Render breakout narrative as a styled HTML section."""
        n = self._breakout_narrative(stock)
        rv = stock.rel_volume
        vol_color = "#27ae60" if rv >= 1.5 else "#f39c12" if rv >= 1.0 else "#e74c3c"

        a = stock.analysis
        sepa_count = 0
        if a and a.sepa_template:
            sepa_count = sum(1 for v in a.sepa_template.values() if v)
        sepa_color = "#27ae60" if sepa_count >= 6 else "#f39c12" if sepa_count >= 4 else "#e74c3c"

        score = a.score if a else 0
        verdict_bg = "#d4edda" if score >= 8 and rv >= 1.5 else "#fff3cd" if score >= 7 else "#f8d7da"
        verdict_border = "#27ae60" if score >= 8 and rv >= 1.5 else "#f39c12" if score >= 7 else "#e74c3c"

        def _row(label: str, text: str, color: str = "#333") -> str:
            return (
                f"<tr><td style='padding:5px 8px;background:#f8f8f8;font-weight:600;"
                f"width:90px;font-size:0.82em;color:#555;vertical-align:top'>{label}</td>"
                f"<td style='padding:5px 8px;color:{color};font-size:0.84em'>{text}</td></tr>"
            )

        return f"""
        <div style="margin:16px 0">
          <div style="font-weight:700;font-size:0.8em;text-transform:uppercase;
                      letter-spacing:0.06em;color:#888;margin-bottom:6px">Breakout Assessment</div>
          <table style="width:100%;border-collapse:collapse;border:1px solid #e0e0e0;border-radius:4px">
            {_row("Volume", n["volume"], vol_color)}
            {_row("SEPA", n["sepa"], sepa_color)}
            {_row("CANSLIM", n["canslim"])}
            {_row("Momentum", n["momentum"])}
          </table>
          <div style="margin-top:8px;padding:10px 14px;background:{verdict_bg};
                      border-left:4px solid {verdict_border};border-radius:0 4px 4px 0;
                      font-weight:700;font-size:0.88em">
            {n["verdict"]}
          </div>
        </div>"""

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
            f"\n{date.today().isoformat()}"
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

        def _row(label: str, text: str, color: str = "#333") -> str:
            return (
                f"<tr><td style='padding:5px 8px;background:#f8f8f8;font-weight:600;"
                f"width:90px;font-size:0.82em;color:#555;vertical-align:top'>{label}</td>"
                f"<td style='padding:5px 8px;color:{color};font-size:0.84em'>{text}</td></tr>"
            )

        rv = stock.rel_volume if stock else None
        vol_color = "#e74c3c" if (rv or 0) >= 1.5 else "#f39c12" if (rv or 0) >= 1.0 else "#27ae60"
        a = stock.analysis if stock else None
        sepa_count = sum(1 for v in (a.sepa_template or {}).values() if v) if a else 0
        trend_color = "#e74c3c" if sepa_count <= 4 else "#f39c12" if sepa_count <= 6 else "#27ae60"

        return f"""
        <html><body style="font-family:Arial,sans-serif;max-width:560px;margin:auto;padding:20px">
          <div style="background:#c0392b;color:white;padding:10px 16px;border-radius:4px;
                      font-weight:bold;margin-bottom:16px;font-size:0.95em">
            &#9888; STOP LOSS HIT: {pos.ticker}
          </div>
          <table style="width:100%;border-collapse:collapse;margin-bottom:16px">
            <tr>
              <td style="padding:6px;background:#f8f8f8"><b>Ticker</b></td>
              <td style="padding:6px">{pos.ticker}</td>
              <td style="padding:6px;background:#f8f8f8"><b>Shares</b></td>
              <td style="padding:6px">{pos.shares}</td>
            </tr>
            <tr>
              <td style="padding:6px;background:#f8f8f8"><b>Current Price</b></td>
              <td style="padding:6px;font-weight:bold">${price:.2f}</td>
              <td style="padding:6px;background:#f8f8f8"><b>Stop Loss</b></td>
              <td style="padding:6px;color:#c0392b;font-weight:bold">${stop:.2f}</td>
            </tr>
            <tr>
              <td style="padding:6px;background:#f8f8f8"><b>Avg Cost</b></td>
              <td style="padding:6px">${pos.avg_cost:.2f}</td>
              <td style="padding:6px;background:#f8f8f8"><b>Unrealised P&amp;L</b></td>
              <td style="padding:6px;color:{pnl_color};font-weight:bold">{pnl_str} ({pnl_pct_str})</td>
            </tr>
          </table>
          <div style="margin:16px 0">
            <div style="font-weight:700;font-size:0.8em;text-transform:uppercase;
                        letter-spacing:0.06em;color:#888;margin-bottom:6px">Situation Assessment</div>
            <table style="width:100%;border-collapse:collapse;border:1px solid #e0e0e0;border-radius:4px">
              {_row("Stop", n["stop"], "#c0392b")}
              {_row("P&amp;L", n["pnl"])}
              {_row("Volume", n["volume"], vol_color)}
              {_row("Trend", n["trend"], trend_color)}
              {_row("CANSLIM", n["canslim"])}
            </table>
            <div style="margin-top:8px;padding:10px 14px;background:{verdict_bg};
                        border-left:4px solid {verdict_border};border-radius:0 4px 4px 0;
                        font-weight:700;font-size:0.88em">
              {n["verdict"]}
            </div>
          </div>
          <p style="color:#aaa;font-size:0.8em;margin-top:24px">
            {date.today().isoformat()} &mdash; Momentum Scanner
          </p>
        </body></html>"""

    def check_portfolio_stops(
        self,
        positions: list[Position],
        stock_map: dict[str, StockRecord],
    ) -> int:
        """Check all open portfolio positions against their stop loss.

        Queues stop-loss alerts into self._sell_alerts for inclusion in the
        summary email. Returns the count of positions breaching their stop.
        """
        self._sell_alerts.clear()
        for pos in positions:
            if pos.stop_loss is None or pos.current_price is None:
                continue
            if pos.current_price > pos.stop_loss:
                continue
            stock = stock_map.get(pos.ticker)
            print(f"\nSTOP LOSS: {pos.ticker} @ ${pos.current_price:.2f} "
                  f"(stop ${pos.stop_loss:.2f})")
            self._sell_alerts.append((pos, stock))

        print(f"\n{len(self._sell_alerts)} stop-loss alert(s) queued.")
        return len(self._sell_alerts)

    def send_summary_email(self, positions: list[Position] | None = None) -> None:
        """Send one consolidated daily summary email.

        Always sends: starts with a portfolio snapshot, then SELL alerts (top),
        then BUY alerts. positions is optional — used for the snapshot section.
        """
        today = date.today().isoformat()
        sell_count = len(self._sell_alerts)
        buy_count = len(self._buy_alerts)

        if sell_count or buy_count:
            subject = (
                f"Stock Alerts {today} — "
                + (f"{sell_count} SELL" + (f", {buy_count} BUY" if buy_count else "") if sell_count else f"{buy_count} BUY")
            )
        else:
            subject = f"Stock Scanner {today} — No alerts"

        # ── Portfolio snapshot (text) ───────────────────────────────────────
        text_parts: list[str] = [f"Stock Scanner Summary — {today}\n{'='*50}"]
        if positions:
            total_value = sum(p.current_value for p in positions if p.current_value)
            total_cost  = sum(p.total_cost for p in positions)
            total_pnl   = total_value - total_cost
            pnl_pct     = total_pnl / total_cost * 100 if total_cost else 0.0
            text_parts.append(
                f"\n\nPORTFOLIO SNAPSHOT\n"
                f"  Positions : {len(positions)}\n"
                f"  Mkt Value : ${total_value:,.2f}\n"
                f"  Cost Basis: ${total_cost:,.2f}\n"
                f"  P&L       : ${total_pnl:+,.2f} ({pnl_pct:+.1f}%)\n"
            )
            for p in positions:
                pnl_str = f"${p.unrealised_pnl:+.2f} ({p.unrealised_pnl_pct:+.1f}%)" if p.unrealised_pnl is not None else "--"
                text_parts.append(f"  {p.ticker:<6} {p.shares:>8.1f} shares  price ${p.current_price or 0:.2f}  P&L {pnl_str}")

        if self._sell_alerts:
            text_parts.append("\n\n*** SELL / STOP LOSS ALERTS ***\n")
            for pos, stock in self._sell_alerts:
                n = self._stop_loss_narrative(pos, stock)
                text_parts.append(self._format_stop_loss_text(pos, stock, n))
                text_parts.append("\n" + "-" * 40)

        def _conviction_rank(item: tuple[StockRecord, str]) -> int:
            verdict = self._breakout_narrative(item[0])["verdict"]
            if "HIGH CONVICTION" in verdict:
                return 0
            if "MODERATE CONVICTION" in verdict:
                return 1
            return 2

        strong_buys = sorted(
            [(s, t) for s, t in self._buy_alerts
             if "LOW CONVICTION" not in self._breakout_narrative(s)["verdict"]],
            key=_conviction_rank,
        )
        if strong_buys:
            text_parts.append("\n\n*** BUY / BREAKOUT ALERTS ***\n")
            for stock, trigger in strong_buys:
                text_parts.append(self.format_alert_text(stock, trigger))
                text_parts.append("\n" + "-" * 40)

        if not self._sell_alerts and not self._buy_alerts:
            text_parts.append("\n\nNo actionable alerts this run.")

        text_body = "\n".join(text_parts)

        # ── Portfolio snapshot (HTML) ───────────────────────────────────────
        snapshot_html = ""
        if positions:
            total_value = sum(p.current_value for p in positions if p.current_value)
            total_cost  = sum(p.total_cost for p in positions)
            total_pnl   = total_value - total_cost
            pnl_pct     = total_pnl / total_cost * 100 if total_cost else 0.0
            pnl_color   = "#27ae60" if total_pnl >= 0 else "#c0392b"
            rows_html = "".join(
                f"<tr>"
                f"<td style='padding:5px 8px;font-weight:700'>{p.ticker}</td>"
                f"<td style='padding:5px 8px;text-align:right'>{p.shares:g}</td>"
                f"<td style='padding:5px 8px;text-align:right'>${p.current_price or 0:.2f}</td>"
                f"<td style='padding:5px 8px;text-align:right'>${p.current_value or 0:,.2f}</td>"
                f"<td style='padding:5px 8px;text-align:right;color:{'#27ae60' if (p.unrealised_pnl or 0) >= 0 else '#c0392b'}'>"
                f"{'%+.1f' % p.unrealised_pnl_pct}%</td>"
                f"</tr>"
                for p in positions if p.current_value
            )
            snapshot_html = f"""
            <div style="margin:16px 0;padding:14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px">
              <div style="font-weight:700;font-size:0.8em;text-transform:uppercase;letter-spacing:0.06em;color:#64748b;margin-bottom:10px">
                Portfolio Snapshot
              </div>
              <div style="display:flex;gap:24px;margin-bottom:10px;flex-wrap:wrap">
                <div><div style="font-size:0.72em;color:#64748b">Market Value</div>
                     <div style="font-weight:700;font-size:1.1em">${total_value:,.2f}</div></div>
                <div><div style="font-size:0.72em;color:#64748b">Cost Basis</div>
                     <div style="font-weight:700;font-size:1.1em">${total_cost:,.2f}</div></div>
                <div><div style="font-size:0.72em;color:#64748b">Unrealised P&amp;L</div>
                     <div style="font-weight:700;font-size:1.1em;color:{pnl_color}">${total_pnl:+,.2f} ({pnl_pct:+.1f}%)</div></div>
              </div>
              <table style="width:100%;border-collapse:collapse;font-size:0.82em">
                <tr style="background:#e2e8f0">
                  <th style="padding:4px 8px;text-align:left">Ticker</th>
                  <th style="padding:4px 8px;text-align:right">Shares</th>
                  <th style="padding:4px 8px;text-align:right">Price</th>
                  <th style="padding:4px 8px;text-align:right">Value</th>
                  <th style="padding:4px 8px;text-align:right">P&amp;L %</th>
                </tr>
                {rows_html}
              </table>
            </div>"""

        # ── HTML body ──────────────────────────────────────────────────────
        html_parts: list[str] = [
            "<html><body style=\"font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px\">"
            f"<h2 style=\"color:#1e3a5f;border-bottom:2px solid #1e3a5f;padding-bottom:8px\">"
            f"&#128200; Stock Scanner Summary &mdash; {today}</h2>"
            + snapshot_html
        ]

        if self._sell_alerts:
            html_parts.append(
                "<div style=\"margin:20px 0\">"
                "<h3 style=\"color:#c0392b;margin-bottom:12px\">&#9888; SELL / STOP LOSS</h3>"
            )
            for pos, stock in self._sell_alerts:
                n = self._stop_loss_narrative(pos, stock)
                html_parts.append(self._sell_card_html(pos, stock, n))
            html_parts.append("</div>")

        if strong_buys:
            html_parts.append(
                "<div style=\"margin:20px 0\">"
                "<h3 style=\"color:#27ae60;margin-bottom:12px\">&#9889; BUY / BREAKOUT</h3>"
            )
            for stock, trigger in strong_buys:
                html_parts.append(self._buy_card_html(stock, trigger))
            html_parts.append("</div>")

        low_conviction_count = len(self._buy_alerts) - len(strong_buys)
        if low_conviction_count:
            html_parts.append(
                f"<p style=\"color:#94a3b8;font-size:0.8em;font-style:italic\">"
                f"{low_conviction_count} low-conviction breakout(s) suppressed.</p>"
            )

        if not self._sell_alerts and not strong_buys:
            html_parts.append(
                "<p style=\"color:#64748b;font-style:italic\">No actionable alerts this run.</p>"
            )

        html_parts.append(
            f"<p style=\"color:#aaa;font-size:0.8em;margin-top:24px\">"
            f"{today} &mdash; Momentum Scanner</p></body></html>"
        )
        html_body = "\n".join(html_parts)

        self.send_email(subject, html_body, text_body)

    @staticmethod
    def _yahoo_url(ticker: str) -> str:
        return f"https://finance.yahoo.com/quote/{ticker.replace('.', '-')}"

    def _sell_card_html(
        self, pos: Position, stock: StockRecord | None, n: dict[str, str]
    ) -> str:
        """Compact HTML card for one stop-loss hit."""
        price = pos.current_price or 0.0
        stop = pos.stop_loss or 0.0
        pnl = pos.unrealised_pnl
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "--"
        pnl_pct = pos.unrealised_pnl_pct
        pnl_pct_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "--"
        pnl_color = "#c0392b" if (pnl or 0) < 0 else "#27ae60"
        verdict_bg = "#f8d7da" if "NOW" in n["verdict"] else "#fff3cd"
        verdict_border = "#e74c3c" if "NOW" in n["verdict"] else "#f39c12"

        url = self._yahoo_url(pos.ticker)
        return f"""
        <div style="border:1px solid #f5c6cb;border-radius:6px;padding:14px;margin-bottom:14px;background:#fff8f8">
          <div style="font-weight:700;font-size:1em;color:#c0392b;margin-bottom:8px">
            &#9888; <a href="{url}" style="color:#c0392b;text-decoration:none" target="_blank">{pos.ticker}</a> &mdash; Stop ${stop:.2f} | Price ${price:.2f}
            <span style="font-weight:400;font-size:0.85em;color:{pnl_color};margin-left:8px">
              P&amp;L {pnl_str} ({pnl_pct_str})
            </span>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:0.82em">
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;width:80px;color:#555;vertical-align:top">Stop</td>
              <td style="padding:4px 6px;color:#c0392b">{n["stop"]}</td>
            </tr>
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;color:#555;vertical-align:top">P&amp;L</td>
              <td style="padding:4px 6px">{n["pnl"]}</td>
            </tr>
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;color:#555;vertical-align:top">Volume</td>
              <td style="padding:4px 6px">{n["volume"]}</td>
            </tr>
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;color:#555;vertical-align:top">Trend</td>
              <td style="padding:4px 6px">{n["trend"]}</td>
            </tr>
          </table>
          <div style="margin-top:8px;padding:8px 12px;background:{verdict_bg};
                      border-left:3px solid {verdict_border};font-weight:700;font-size:0.85em">
            {n["verdict"]}
          </div>
        </div>"""

    def _buy_card_html(self, stock: StockRecord, trigger: str) -> str:
        """Compact HTML card for one breakout alert."""
        a = stock.analysis
        assert a is not None
        score_color = "#27ae60" if a.score >= 8 else "#f39c12" if a.score >= 6 else "#e74c3c"
        entry = f"${a.entry_price:.2f}" if a.entry_price else "—"
        stop = f"${a.stop_loss:.2f}" if a.stop_loss else "—"
        if a.entry_price and a.stop_loss:
            risk_pct = (a.entry_price - a.stop_loss) / a.entry_price * 100
            risk_str = f"{risk_pct:.1f}%"
        else:
            risk_str = "—"
        n = self._breakout_narrative(stock)
        verdict_bg = "#d4edda" if a.score >= 8 and stock.rel_volume >= 1.5 else "#fff3cd" if a.score >= 7 else "#f8d7da"
        verdict_border = "#27ae60" if a.score >= 8 and stock.rel_volume >= 1.5 else "#f39c12" if a.score >= 7 else "#e74c3c"
        url = self._yahoo_url(stock.ticker)

        return f"""
        <div style="border:1px solid #c3e6cb;border-radius:6px;padding:14px;margin-bottom:14px;background:#f8fff9">
          <div style="font-weight:700;font-size:1em;color:#1e3a5f;margin-bottom:4px">
            &#9889; <a href="{url}" style="color:#1e3a5f;text-decoration:none;font-weight:800" target="_blank">{stock.ticker}</a>
            <span style="color:{score_color};font-size:0.88em;margin-left:6px">
              Score {a.score}/10 &mdash; {a.stage}
            </span>
            <span style="background:#1e3a5f;color:white;padding:2px 8px;border-radius:3px;
                         font-size:0.75em;margin-left:8px">{trigger}</span>
          </div>
          <div style="font-size:0.82em;color:#555;margin-bottom:8px">
            Price <b>${stock.price}</b> &nbsp;|&nbsp;
            Entry <b>{entry}</b> &nbsp;|&nbsp;
            Stop <b>{stop}</b> &nbsp;|&nbsp;
            Risk <b>{risk_str}</b> &nbsp;|&nbsp;
            RSI <b>{stock.rsi14}</b> &nbsp;|&nbsp;
            RelVol <b>{stock.rel_volume}x</b>
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:0.82em">
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;width:80px;color:#555;vertical-align:top">Volume</td>
              <td style="padding:4px 6px">{n["volume"]}</td>
            </tr>
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;color:#555;vertical-align:top">SEPA</td>
              <td style="padding:4px 6px">{n["sepa"]}</td>
            </tr>
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;color:#555;vertical-align:top">CANSLIM</td>
              <td style="padding:4px 6px">{n["canslim"]}</td>
            </tr>
            <tr>
              <td style="padding:4px 6px;background:#f8f8f8;font-weight:600;color:#555;vertical-align:top">Momentum</td>
              <td style="padding:4px 6px">{n["momentum"]}</td>
            </tr>
          </table>
          <div style="margin-top:8px;padding:8px 12px;background:{verdict_bg};
                      border-left:3px solid {verdict_border};font-weight:700;font-size:0.85em">
            {n["verdict"]}
          </div>
          {self._svg_chart(stock.price_history, score_color)}
        </div>"""

    def send_email(self, subject: str, html_body: str, text_body: str) -> None:
        if not self.email_config.user or not self.email_config.password or not self.email_config.recipient:
            print("  [email] (not configured)")
            return

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
        except Exception as error:
            print(f"  [email] error: {error}")


if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).parent.parent.parent))

    analysis_file = _Path(__file__).parent.parent / "analyst" / "analysis_results.json"
    agent = AlertAgent()
    with open(analysis_file, encoding="utf-8") as handle:
        payload = [StockRecord.model_validate(item) for item in json.load(handle)]
    agent.run(payload)
