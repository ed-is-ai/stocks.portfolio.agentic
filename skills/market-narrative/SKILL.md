---
name: market-narrative
description: Use this skill to produce a short, factual market-context blurb — one headline plus a few citation-backed bullets — for a stock-portfolio digest email or web banner. Use when generating a market narrative from a scan run's deterministic figures (sector allocation and its week-on-week shift, high-conviction 7/10+ prevalence, multi-year base breakouts, S&P 500 breadth, congressional net buying, FOMC cycle position) grounded in a supplied set of recent news headlines. Enforces strict citation guardrails so no outlet, event, or claim outside the supplied headlines is ever named, and always carries a not-financial-advice note.
---

# Market Narrative

## Overview

This skill packages the recipe for the market-context blurb shown atop the digest email
and the web watchlist banner. Given a set of deterministic, trustworthy figures from a
scan run plus a handful of recent news headlines, it produces a `MarketNarrative`: one
headline, a few bullets reading the market's likely cycle position and risk posture, and
source citations for any bullet that draws on the news.

The narrative is deliberately **provider-agnostic**. Two builders produce the same output
shape:

- an **LLM builder** (Claude Sonnet 5) — news-grounded, hallucination-guarded; and
- a **deterministic builder** — rule-based, network-free, always available.

The pipeline tries the LLM first and falls back to the deterministic narrative on any
degraded path, so the digest never fails because the model is unset, rate-limited, or
unreachable.

**Key principle:** every news claim must trace to a supplied headline. The model may state
the supplied figures (sector, breadth, congress, cycle, breakouts) directly, but must not
name any outlet, publication, event, or news claim absent from the fed headlines. A
mechanical guard enforces this after generation.

## When to Use This Skill

Use this skill when:

- Generating the market-context header for a portfolio digest email or web banner from a
  scan run's sector-allocation, breadth, congressional-flow, FOMC-cycle, and news figures.
- You need a headline + a few hedged, citation-backed bullets describing sector rotation
  and the market's likely risk posture.
- You need the citation/hedging/not-advice guardrails applied to a model-written summary.

**Do NOT use when:**

- Analysing an individual chart or stock (use `technical-analyst` or the screener skills).
- You want raw sector or breadth numbers without narrative framing — read those figures
  directly.
- No trustworthy input figures are available; this skill summarises supplied facts, it
  does not source them.

## Prerequisites

- **`ANTHROPIC_API_KEY`** — required for the LLM builder. Absent it, the deterministic
  builder runs instead (no network, no key). News is fetched only when the key is set.
- **Input context** — the deterministic figures and (for the LLM path) recent headlines,
  assembled as described in [`references/input_context.md`](references/input_context.md).
  In the app these come from the scan run: sector allocation, portfolio weights, market
  breadth, congressional buys, market cycle, and news context.

## Inputs

Assembled into a single plain-text user prompt (full format in
[`references/input_context.md`](references/input_context.md)):

1. Sector prevalence this run — share of candidates, high-conviction 7/10+ share, MYB count.
2. Week-on-week sector-prevalence shift (over a ~7-day lookback), or a first-run note.
3. Sectors with the most multi-year base breakouts.
4. S&P 500 market breadth — % of members above their 200-day average (optional).
5. Stocks most heavily net-bought by Congress / Senate (optional).
6. Current portfolio sector weights (optional).
7. FOMC market-cycle phase and day offsets.
8. Recent news headlines — `title | domain | url | sentiment` (LLM path only).

## Output

A `MarketNarrative` (see [`references/output_schema.md`](references/output_schema.md)):

- `headline` — one line.
- `bullets` — a few flat-text bullets; only guard-approved ones survive.
- `sources` — citations built from the domains grounding the kept bullets (empty for
  deterministic output).
- `not_advice` — always present.
- `generated_at` — ISO timestamp.

## Workflow

1. **Assemble context.** Gather the deterministic figures. When the LLM is enabled, also
   gather recent headlines (Alpha Vantage + GDELT, deduped and recency-ranked). Render
   them into the user prompt per [`references/input_context.md`](references/input_context.md).
2. **Call the model** with the system prompt in
   [`references/system_prompt.md`](references/system_prompt.md), thinking disabled, and the
   response constrained to the JSON schema in
   [`references/output_schema.md`](references/output_schema.md). One call per run.
3. **Guard the draft.** Run the citation checks in
   [`references/guardrails.md`](references/guardrails.md): reject the whole draft if the
   headline names an un-fed outlet; drop any bullet citing an un-fed domain or naming an
   un-fed outlet; collect the kept bullets' domains as sources.
4. **Render `MarketNarrative`** from the surviving headline, bullets, and sources.
5. **Fall back to deterministic** whenever the LLM path is unavailable or the guard
   rejects everything — the deterministic builder produces the same shape from the same
   figures (minus news), with `sources=[]`.

## Guardrails

Summarised here; full rules in [`references/guardrails.md`](references/guardrails.md):

- **Cite everything from news** — every news-derived bullet lists the exact fed domain(s)
  in `cited_domains`; figure-only bullets leave it empty.
- **No un-fed outlets** — bullets naming a known outlet absent from the fed headlines are
  dropped; a headline doing so rejects the whole draft.
- **Hedged, never certain** — cycle/risk reads are "likely" / "consistent with", and the
  blurb closes with a hedged short-term direction/risk summary.
- **Not advice** — the rendered `MarketNarrative.not_advice` note is always present.

## Relationship to the app

The runtime implementation lives in the app and is unchanged by this skill:

- System prompt + schema + model call: `app/integrations/anthropic_client.py`
- Two builders + persistence: `app/agents/scanner/market_narrative.py`
- Citation guard: `app/agents/scanner/narrative_guard.py`
- Context assembly: `app/agents/scanner/{sector_allocation,news_context,market_cycle}.py`,
  `app/integrations/market_breadth.py`
- Assembled and invoked in: `app/orchestration/orchestrator.py`

This skill is the versioned, discoverable **source of truth for the recipe**. A drift-guard
test (`tests/test_market_narrative.py`) keeps [`references/system_prompt.md`](references/system_prompt.md)
byte-for-byte identical to the app's live `_SYSTEM_PROMPT`.

## Resources

- [`references/system_prompt.md`](references/system_prompt.md) — the verbatim system prompt
  and model-call parameters.
- [`references/input_context.md`](references/input_context.md) — the user-prompt context
  format and where each figure originates.
- [`references/output_schema.md`](references/output_schema.md) — the raw JSON schema, the
  parsed draft, and the rendered `MarketNarrative` shape.
- [`references/guardrails.md`](references/guardrails.md) — the citation, hedging, and
  fallback rules.
