---
name: rtly-position-thesis
description: Use this skill to draft a holding thesis for one security held in a personal stock portfolio — a short rationale, an expected setup and one to six typed invalidation rules — from anonymised stock-scanner evidence only. Use when the portfolio app's thesis editor asks Claude for an AI draft. The security is only ever called "Security A", prices appear only as percent distances, and no ticker, price, currency amount, share count, cost basis or cash is ever sent. Rules come from a closed vocabulary (close_below_stop, close_below_sma, stage_2_lost, score_below); the draft stays inactive until the user confirms it, and a deterministic evaluator — never the model — checks it after each scan.
---

# Position Thesis

## Overview

This skill packages the recipe behind the Portfolio tab's **Thesis** column. A thesis
records why the user holds a security and what would prove that reason wrong:

- `rationale` — why the evidence supports holding the security;
- `expected_setup` — what should happen next if the thesis is right;
- `rules` — one to six typed invalidation rules;
- `review_date` — optional; a past date shows "Review due" but never changes the status.

Claude can **draft** a thesis for one holding per user click. A draft is stored as an
inactive `ai_draft` version and only becomes the active thesis when the user confirms it.
After every published scan a **deterministic evaluator** (no LLM) checks each active thesis
against the published evidence and records `confirmed`, `weakened`, `invalidated` or
`evidence_limited`, citing the rule and the scan fields it read.

**Key principle:** the model proposes wording and rule parameters only. It never sees
position data, never decides the status, and never touches trades.

## When to Use This Skill

Use this skill when:

- The user clicks **Draft with AI** in the thesis editor for an open holding.
- You need the prompt, schema and guardrails for drafting a holding thesis from
  anonymised scanner evidence.

**Do NOT use when:**

- Evaluating a thesis — that is the deterministic evaluator's job.
- Drafting for every holding automatically — drafting is per user click only.
- Monitoring news or transcripts — out of scope.

## Prerequisites

- **`ANTHROPIC_API_KEY`** — required. Without it, or on any failure, the editor shows
  "AI draft unavailable" and nothing is written.
- A published analysis record for the security. With no record, no call is made.

## Inputs

One plain-text user prompt, built only from `build_evidence(record, is_held=True, ...)`
(`app/agents/research/evidence.py`): numbered `E1…En` items, each with its kind, date and
source, and every limitation (stale data, failed sources, withheld items). Full format in
[`references/system_prompt.md`](references/system_prompt.md).

## Output

JSON constrained to the draft schema (`rationale`, `expected_setup`, `rules`), where each
rule's `kind` is an enum of the vocabulary:

| kind | parameters | fires when |
|------|------------|------------|
| `close_below_stop` | — | scan price < analysis stop loss |
| `close_below_sma` | `period` 50/150/200, optional `min_rel_volume` ≥ 1.0 | price < SMA (and relative volume ≥ the minimum, when set) |
| `stage_2_lost` | — | analysis stage is not Stage 2 |
| `score_below` | `min_score` 1–10 | analysis score < `min_score` |

No rule takes a price, so a draft never needs a price level.

## Workflow

1. **Check the holding.** The security must be an open holding of the portfolio.
2. **Build evidence** with `build_evidence(record, is_held=True, ...)` — label "Security A",
   % distances, currency amounts dropped.
3. **Call the model** with the system prompt in
   [`references/system_prompt.md`](references/system_prompt.md): `claude-sonnet-5`, thinking
   disabled, JSON-schema output, `stop_reason == "end_turn"` required.
4. **Validate.** Each rule is validated into `ThesisRuleV1`; invalid rules are dropped, and a
   draft with zero valid rules is unavailable. The wording goes through `reveal` so the
   ticker is restored locally.
5. **Store** the draft as an inactive `ai_draft` version. The user confirms it to activate it,
   which also evaluates it at once.

## Guardrails

- **Anonymised input only** — nothing about the position beyond `build_evidence` output: no
  ticker, prices, amounts, share counts, cost basis or cash. A test asserts the built prompt
  contains no ticker and no currency amount.
- **Closed vocabulary** — no free-text rules; unknown kinds or out-of-range parameters are
  dropped.
- **Fail soft** — no key, a refusal, truncation, bad JSON or zero valid rules all return
  "AI draft unavailable" and write nothing.
- **Human confirmation** — AI wording is labelled "AI-drafted wording" and stays inactive
  until confirmed; the user's own edits are labelled "Your wording".
- **No trades** — theses never create, edit or submit trades.

## Relationship to the app

- Prompt, schema, client and validation: `app/agents/thesis/drafter.py`
- Deterministic evaluator: `app/agents/thesis/evaluator.py`
- Schemas: `app/schemas/position_thesis.py`
- Storage: `app/repositories/position_theses_repo.py` (`position_theses`, `thesis_evaluations`)
- Service: `app/services/position_thesis_service.py`
- Routes and editor: `app/api/routes/theses.py`, `app/api/templates/_thesis_editor.html`
- Post-scan evaluation: `evaluate_position_theses` in `app/orchestration/orchestrator.py`

A drift-guard test (`tests/test_thesis_drafter.py`) keeps
[`references/system_prompt.md`](references/system_prompt.md) byte-for-byte identical to the
app's live `_SYSTEM_PROMPT`.

## Resources

- [`references/system_prompt.md`](references/system_prompt.md) — the verbatim system prompt,
  the user-prompt format and the model-call parameters.
