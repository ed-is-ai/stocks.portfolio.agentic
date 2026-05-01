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

from ms_agent_framework import Agent
from models import EmailConfig, StockRecord


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
        alerted_count = 0

        for stock in results:
            if not self.should_alert(stock, conn):
                continue

            trigger = self.alert_trigger(stock)
            text_msg = self.format_alert_text(stock, trigger)
            html_msg = self.format_alert_html(stock, trigger)
            subject = f"{trigger}: {stock.ticker} | {stock.analysis.stage} {stock.analysis.score}/10"

            print(f"\nALERT: {stock.ticker} [{trigger}]")
            print(text_msg)
            self.send_email(subject, html_msg, text_msg)
            self.record_alert(conn, stock)
            alerted_count += 1

        conn.close()
        print(f"\n{alerted_count} alert(s) fired.")
        return alerted_count

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

        trigger_line = f"** {trigger} **\n\n" if trigger else ""
        body = (
            f"{trigger_line}{stock.ticker} — Score {analysis.score}/10 | {analysis.stage}\n"
            f"Price: ${stock.price}  |  RSI: {stock.rsi14}  |  RelVol: {stock.rel_volume}x\n"
            f"Distance from 52w high: {stock.pct_from_52w_high}%\n"
            f"Entry: {entry}  |  Stop: {stop}  |  Risk: {risk_str}\n"
            f"\n{analysis.summary}"
            f"{canslim_str}\n"
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
