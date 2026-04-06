"""
Analyst Agent — scores each stock against CANSLIM / Weinstein Stage 2 criteria.
Uses Foundry Local (phi-4-mini via OpenAI-compatible API) for commentary and
falls back to rule-based scoring.
"""

import json
from typing import Iterable

import openai

from ms_agent_framework import Agent
from models import StockAnalysis, StockRecord


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


class AnalystAgent(Agent):
    name: str = "AnalystAgent"

    def run(self, payload: Iterable[StockRecord]) -> list[StockRecord]:
        scan_results = list(payload)
        client = self.get_llm_client()
        if client:
            print(f"Using Foundry Local ({FOUNDRY_MODEL}) for analysis")
        else:
            print("Foundry Local unavailable — using rule-based scoring")

        analysis_results: list[StockRecord] = []
        for stock in scan_results:
            try:
                analysis = self.score_stock(stock, client)
                stock.analysis = analysis
                analysis_results.append(stock)
                print(
                    f"  {stock.ticker}: {analysis.score}/10 — {analysis.stage} "
                    f"— near_entry={analysis.near_entry}"
                )
            except Exception as error:
                print(f"  [err] {stock.ticker}: {error}")
        return sorted(
            analysis_results,
            key=lambda record: record.analysis.score if record.analysis else 0,
            reverse=True,
        )

    def get_llm_client(self) -> openai.OpenAI | None:
        try:
            client = openai.OpenAI(base_url=FOUNDRY_BASE_URL, api_key="foundry-local")
            client.models.list()
            return client
        except Exception:
            return None

    def score_stock(self, stock: StockRecord, client: OpenAI | None) -> StockAnalysis:
        if client:
            return self.score_stock_llm(stock, client)
        return self.rule_based_score(stock)

    def score_stock_llm(self, stock: StockRecord, client: OpenAI) -> StockAnalysis:
        prompt = SCORE_PROMPT.format(data=json.dumps(stock.model_dump(), indent=2))
        response = client.chat.completions.create(
            model=FOUNDRY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=400,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        payload = json.loads(raw)
        return StockAnalysis.model_validate(payload)

    def rule_based_score(self, stock: StockRecord) -> StockAnalysis:
        score = 5
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
            score += 1
            strengths.append("Price above SMA150 & SMA200 (Stage 2 structure)")
        if price > sma50:
            score += 1
            strengths.append("Price above SMA50")
        if -15 <= pct_from_high <= -1:
            score += 1
            strengths.append(f"Within {abs(pct_from_high):.0f}% of 52w high")
        if 50 <= rsi <= 80:
            score += 1
            strengths.append(f"RSI {rsi:.0f} — healthy momentum range")
        elif rsi > 80:
            risks.append(f"RSI {rsi:.0f} — potentially overbought")
            score -= 1
        elif rsi < 40:
            risks.append(f"RSI {rsi:.0f} — weak momentum")
            score -= 1
        if rel_vol >= 1.5:
            score += 1
            strengths.append(f"Relative volume {rel_vol:.1f}x — institutional interest")
        elif rel_vol < 0.7:
            risks.append(f"Low relative volume ({rel_vol:.1f}x)")
            score -= 1
        if pct_from_high < -30:
            risks.append(f"Far from 52w high ({pct_from_high:.0f}%)")
            score -= 1

        score = max(1, min(10, score))
        near_entry = -5 <= pct_from_high <= 0
        if price > sma150 > sma200 and price > sma50:
            stage = "Stage 2"
        elif price < sma150 and price < sma200:
            stage = "Stage 4"
        elif price > sma200 and price < sma150:
            stage = "Stage 1"
        else:
            stage = "Stage 3"

        return StockAnalysis(
            score=score,
            stage=stage,
            near_entry=near_entry,
            strengths=strengths[:3],
            risks=risks[:2],
            summary=f"Rule-based score {score}/10. {stage} structure.",
        )


if __name__ == "__main__":
    with open("scan_results.json", encoding="utf-8") as handle:
        scan_data = json.load(handle)

    results = AnalystAgent().run([StockRecord.model_validate(item) for item in scan_data])

    with open("analysis_results.json", "w", encoding="utf-8") as handle:
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
