---
name: rtly-trade-review
description: Use this skill to interpret one week of trade-process reviews for a personal stock portfolio — a short summary of how closely the week's trades followed the process and any recurring deviations — from anonymised checklist facts only. Use when the History tab's weekly process summary asks Claude for a model interpretation. Trades are only ever "Trade A", "Trade B", ...; checks appear as kind, status, rule id, evidence session and percentages (entry vs pivot, risk to the stop); no ticker, price, amount, share count, cost basis or annotation text is ever sent. The checklist itself is deterministic — the model never decides a status, and nothing it returns is stored.
---

# Trade Review

## Overview

This skill packages the recipe behind the History tab's **Process** column and weekly
summary. A deterministic checklist reviews every BUY and SELL using only evidence dated
strictly before the trade date, and classifies each check as `followed`, `deviated`,
`unknown` (evidence missing — never a mistake) or `n_a`:

| check | applies to | followed when |
|-------|------------|---------------|
| `valid_setup` | BUY | Weinstein stage at the prior session is Stage 2 (252 sessions) |
| `entry_location` | BUY | pivot ≤ entry ≤ pivot × 1.05 (latest committed scan before the trade) |
| `evidenced_stop` | BUY | risk to the recorded or annotated stop ≤ 8% |
| `exit_signal` | SELL, open BUY lot | sold within 5 sessions of the first close below SMA50 or at/below the stop |
| `strategy_alignment` | BUY, SELL | `n_a` with no Strategy then; `unknown` otherwise (replay not evaluated) |
| `data_completeness` | BUY, SELL | every other check had its evidence |

The weekly summary shows three separate parts: **Facts** (deterministic counts and
recurring deviations), **Your annotations** (verbatim) and **Model interpretation** — this
skill — only when the user clicks.

**Key principle:** the model reads the anonymised facts and describes patterns. It never
sees position data, never decides a status, never judges by outcome, and never gives
trade, psychological or personal advice.

## When to Use This Skill

Use this skill when:

- The user clicks **Interpret with AI** in the History tab's weekly process summary.
- You need the prompt, schema and guardrails for interpreting a week of process reviews.

**Do NOT use when:**

- Reviewing a trade — that is the deterministic checklist's job.
- Replaying a Strategy against past trades — out of scope.
- Changing a Strategy or a trade — the review never writes either.

## Prerequisites

- **`ANTHROPIC_API_KEY`** — required. Without it, or on any failure, the strip shows
  "Interpretation unavailable" while the facts and annotations still show.
- At least one reviewed trade in the week.

## Inputs

One plain-text user prompt built by `build_prompt` (`app/agents/trade_review/weekly.py`)
from `WeeklyFactsV1` and the week's `TradeReviewV1` rows only. Full format in
[`references/system_prompt.md`](references/system_prompt.md).

## Output

JSON constrained to `summary` (string) and `patterns` (array of strings), validated into
`TradeReviewInterpretationV1`. Nothing is stored; it is shown once, labelled as model
interpretation.

## Workflow

1. **Build the week** with `TradeReviewService.weekly(week)` — reviews, facts, labels.
2. **Anonymise** with `build_prompt` — trade labels, check kinds, statuses, rule ids,
   sessions and whitelisted percentages or session counts only.
3. **Call the model** with the system prompt in
   [`references/system_prompt.md`](references/system_prompt.md): `claude-sonnet-5`, thinking
   disabled, JSON-schema output, `stop_reason == "end_turn"` required.
4. **Validate** into `TradeReviewInterpretationV1`; wording stating a currency amount is
   discarded.

## Guardrails

- **Anonymised input only** — a test asserts the full serialised request contains no
  ticker, price, stop level, amount, share count or annotation text.
- **No look-ahead** — every checklist read is bounded to the session before the trade;
  scans come from `latest_committed_scan_result(..., as_of_session=D-1)`.
- **Read-only evidence** — the price cache and backtest store are opened `mode=ro`.
- **Fail soft** — no key, a refusal, truncation or bad JSON show "Interpretation
  unavailable" and write nothing.
- **Outcome-blind** — no check reads an exit price or P&L.

## Relationship to the app

- Prompt, schema, client and weekly facts: `app/agents/trade_review/weekly.py`
- Deterministic checklist: `app/agents/trade_review/checklist.py`
- Read-only as-of evidence: `app/agents/trade_review/evidence.py`
- Schemas: `app/schemas/trade_review.py`
- Annotations: `app/repositories/trade_annotations_repo.py` (`trade_annotations`)
- Strategy history: `app/repositories/portfolio_strategies_repo.py`
  (`portfolio_strategy_history`)
- Service: `app/services/trade_review_service.py`
- Routes and templates: `app/api/routes/trade_review.py`, `_trade_process.html`,
  `_trade_review_modal.html`, `_trade_weekly.html`

A drift-guard test (`tests/test_trade_review_weekly.py`) keeps
[`references/system_prompt.md`](references/system_prompt.md) byte-for-byte identical to the
app's live `_SYSTEM_PROMPT`.

## Resources

- [`references/system_prompt.md`](references/system_prompt.md) — the verbatim system prompt,
  the user-prompt format and the model-call parameters.
