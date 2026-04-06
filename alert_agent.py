"""
Alert Agent — sends email notifications when a stock scores above threshold.
Reads analysis results and fires alerts for near-entry setups.
"""

import json
import os
import smtplib
import sqlite3
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable

from ms_agent_framework import Agent
from models import EmailConfig, StockRecord


SCORE_THRESHOLD = 7
NEAR_ENTRY_ONLY = True
ALERT_COOLDOWN_HOURS = 24
DB_PATH = "alerts.db"

EMAIL_CONFIG = EmailConfig(
    host=os.getenv("EMAIL_HOST", "smtp.gmail.com"),
    port=int(os.getenv("EMAIL_PORT", "587")),
    user=os.getenv("EMAIL_USER", ""),
    password=os.getenv("EMAIL_PASSWORD", ""),
    recipient=os.getenv("EMAIL_TO", ""),
)


class AlertAgent(Agent):
    name: str = "AlertAgent"
    db_path: str = DB_PATH
    email_config: EmailConfig = EMAIL_CONFIG

    def run(self, payload: Iterable[StockRecord]) -> int:
        results = list(payload)
        conn = self.init_db()
        alerted_count = 0

        for stock in results:
            if not self.should_alert(stock, conn):
                continue

            text_msg = self.format_alert_text(stock)
            html_msg = self.format_alert_html(stock)
            subject = f"Momentum Alert: {stock.ticker} — {stock.analysis.score}/10 | {stock.analysis.stage}"

            print(f"\nALERT: {stock.ticker}")
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
        conn.commit()
        return conn

    def was_recently_alerted(self, conn: sqlite3.Connection, ticker: str) -> bool:
        row = conn.execute(
            "SELECT alerted_at FROM alerts WHERE ticker=? ORDER BY alerted_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return False
        last = row[0][:13]
        diff = datetime.utcnow() - datetime.fromisoformat(last + ":00:00")
        return diff.total_seconds() < ALERT_COOLDOWN_HOURS * 3600

    def record_alert(self, conn: sqlite3.Connection, stock: StockRecord) -> None:
        conn.execute(
            "INSERT INTO alerts (ticker, alerted_at, score, stage, summary) VALUES (?, ?, ?, ?, ?)",
            (
                stock.ticker,
                datetime.utcnow().isoformat(),
                stock.analysis.score,
                stock.analysis.stage,
                stock.analysis.summary,
            ),
        )
        conn.commit()

    def should_alert(self, stock: StockRecord, conn: sqlite3.Connection) -> bool:
        if not stock.analysis:
            return False
        if stock.analysis.score < SCORE_THRESHOLD:
            return False
        if NEAR_ENTRY_ONLY and not stock.analysis.near_entry:
            return False
        if self.was_recently_alerted(conn, stock.ticker):
            print(f"  [skip] {stock.ticker}: already alerted recently")
            return False
        return True

    def format_alert_text(self, stock: StockRecord) -> str:
        analysis = stock.analysis
        assert analysis is not None
        strengths = "\n".join(f"  + {item}" for item in analysis.strengths)
        risks = "\n".join(f"  - {item}" for item in analysis.risks)

        body = (
            f"{stock.ticker} — Score {analysis.score}/10 | {analysis.stage}\n"
            f"Price: ${stock.price}  |  RSI: {stock.rsi14}  |  RelVol: {stock.rel_volume}x\n"
            f"Distance from 52w high: {stock.pct_from_52w_high}%\n"
            f"\n{analysis.summary}\n"
            f"\n{strengths}"
        )
        if risks:
            body += f"\n{risks}"
        body += f"\n\n{date.today().isoformat()}"
        return body

    def format_alert_html(self, stock: StockRecord) -> str:
        analysis = stock.analysis
        assert analysis is not None
        strengths_html = "".join(f"<li>&#10003; {item}</li>" for item in analysis.strengths)
        risks_html = "".join(f"<li>&#9888; {item}</li>" for item in analysis.risks)

        score_color = "#27ae60" if analysis.score >= 8 else "#f39c12" if analysis.score >= 6 else "#e74c3c"
        risks_section = (
            f"<ul style='padding-left:20px;color:#c0392b'>{risks_html}</ul>" if risks_html else ""
        )

        return f"""
        <html><body style=\"font-family:Arial,sans-serif;max-width:520px;margin:auto;padding:20px\">
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
          </table>
          <p style=\"font-style:italic;color:#555\">{analysis.summary}</p>
          <ul style=\"padding-left:20px;color:#27ae60\">{strengths_html}</ul>
          {risks_section}
          <p style=\"color:#aaa;font-size:0.8em;margin-top:24px\">{date.today().isoformat()} &mdash; Momentum Scanner</p>
        </body></html>
        """

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
    agent = AlertAgent()
    with open("analysis_results.json", encoding="utf-8") as handle:
        payload = [StockRecord.model_validate(item) for item in json.load(handle)]
    agent.run(payload)
