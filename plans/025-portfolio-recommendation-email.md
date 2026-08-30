# Plan 025: One daily Strategy recommendation email per assigned portfolio (story #442)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat c12ef330..HEAD -- app/orchestration/orchestrator.py app/agents/alert/alert_agent.py app/agents/alert/templating.py app/repositories/db.py plans/024-portfolio-recommendations-screen.md`
> If anchors changed since `c12ef330`, compare the "Current state" excerpts
> against the live code before proceeding; on a mismatch, treat it as a STOP
> condition. Plans 023 and 024 must be DONE before starting.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: 023, 024
- **Category**: feature
- **Planned at**: commit `c12ef330`, 2026-08-30
- **GitHub story**: ed-is-ai/Agents.stocks#442 (parent feature #439)
- **UX reference**: `docs/mockups/portfolio-strategy-recommendations.html`
  (surface 4)

## Why this matters

Each Strategy-assigned portfolio should receive its own daily recommendation
email — market summary, that portfolio's summary, then Sell/Hold/Buy actions —
built from the same typed result the screen shows (plan 024), sent after the
existing consolidated digest, idempotent per (portfolio, scan run), and
failure-isolated so one portfolio's SMTP or evaluation problem never aborts
the pipeline, other portfolios' emails, or the existing digest.

## Current state

- Consolidated email call site: `app/orchestration/orchestrator.py` — the
  orchestrator constructs `alerter = AlertAgent(name="AlertAgent")` (~line
  1046), and after per-portfolio stop checks calls
  `alerter.send_summary_email(positions, gbp_totals=gbp_totals,
  market_narrative=market_narrative)` (~line 1180). The per-portfolio loop
  just above already builds portfolio-scoped positions via
  `_trader.list_portfolios()` → `_trader.get_portfolio(...)` — the natural
  seam for per-portfolio dispatch.
- `AlertAgent` (`app/agents/alert/alert_agent.py:131`) is a pydantic model;
  SMTP path is `smtplib.SMTP` → `starttls` → `login` → `send_message`
  (MIMEMultipart text+HTML) inside `send_summary_email`'s send helper
  (~line 1465). It carries mutable `_buy_alerts`/`_sell_alerts` state — new
  email code must not couple to it.
- Templates: module-level Jinja2 env `email_templates` in
  `app/agents/alert/templating.py` (`FileSystemLoader(ALERT_TEMPLATES_DIR)`,
  autoescape, trim/lstrip blocks) + `get_macro(template, macro)`. Existing
  partials to reuse: `_snapshot.html`, `_market_narrative.html`,
  `_sell_card.html`, `_buy_card.html`, `_macros.html`. Emails are table-based
  inline-styled HTML (Arial, 600px, `#1e3a5f` headings).
- Idempotency precedents: import receipts in `trades.db`
  (`portfolio_import_receipts` with digests); composite PKs
  (`fx_rate_cache (pair, date)`). There is **no email dispatch-receipt
  table** yet.
- `PortfolioStrategiesRepository.list_assigned()` (plan 023) and
  `PortfolioRecommendationService.recommend(portfolio_id) ->
  RecommendationResultV1` (plan 024) are the upstream contracts.
- Tests: `tests/test_email_portfolio_parity.py` and the existing
  daily-email tests must keep passing unchanged.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| New tests | `uv run pytest tests/test_portfolio_recommendation_email.py -v` | all pass |
| Existing email tests | `uv run pytest tests/test_email_portfolio_parity.py -v` | all pass (unchanged) |
| Full suite | `uv run pytest` | all pass |
| Lint | `uv run ruff check app/ tests/` | All checks passed! |
| Format | `uv run ruff format app/ tests/` | unchanged/reformatted |
| Types | `uv run pyrefly check` | no new errors |

## Scope

**In scope** (the only files you should create/modify):
- `app/repositories/db.py` — add `portfolio_recommendation_dispatches` table
- `app/repositories/portfolio_dispatch_repo.py` — **new**
- `app/agents/alert/templates/portfolio_recommendation.html` — **new**
- `app/agents/alert/templates/_recommendation_row.html` — **new** (if card
  partials don't fit rows; prefer reusing `_sell_card.html`/`_buy_card.html`
  where they do)
- `app/agents/alert/alert_agent.py` — add
  `send_portfolio_recommendation_email(result: RecommendationResultV1,
  portfolio_name: str) -> bool` (sibling method; SMTP helper reused)
- `app/services/portfolio_recommendation_email_service.py` — **new**
  orchestration of dispatch + receipts
- `app/orchestration/orchestrator.py` — invoke dispatch after the existing
  digest send
- `app/api/dependencies.py` — add `get_portfolio_dispatch_repository` (and
  email service provider if the orchestrator takes it via DI; the
  orchestrator currently constructs agents inline — follow its local pattern)
- `tests/test_portfolio_recommendation_email.py` — **new**

**Out of scope** (do NOT touch):
- `send_summary_email` behaviour, subject, or templates (existing digest is
  unchanged; its tests must pass unmodified)
- `AlertAgent._buy_alerts`/`_sell_alerts` and the alert/cooldown pipeline
- Per-portfolio email recipients, intraday/assignment-triggered sends
- Action-rule recalculation in email code (consume `RecommendationResultV1`)

## Git workflow

- Branch: `feat/025-portfolio-recommendation-email`
- Commit message: `feat(alert): per-portfolio daily strategy recommendation email`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Dispatch-receipt table + repository

In `app/repositories/db.py` `_SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS portfolio_recommendation_dispatches (
    portfolio_id     INTEGER NOT NULL,
    analysis_run_id  TEXT NOT NULL,
    strategy_id      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'claimed',
    claimed_at       TEXT NOT NULL,
    completed_at     TEXT,
    PRIMARY KEY (portfolio_id, analysis_run_id)
);
```

New `app/repositories/portfolio_dispatch_repo.py` with an atomic
claim/success lifecycle:

- `claim(portfolio_id, analysis_run_id, strategy_id) -> bool` —
  `INSERT OR IGNORE` in a transaction; returns `False` if a row already
  exists for the pair (idempotency: retrying/restarting the same published
  scan cannot resend, AC #442.7). A later `analysis_run_id` claims fresh.
- `mark_sent(portfolio_id, analysis_run_id) -> None` — sets
  `status='sent'`, `completed_at`.
- `mark_failed(portfolio_id, analysis_run_id) -> None` — sets
  `status='failed'` (a failed claim may be retried by a later run; the
  primary key still prevents duplicate sends within one run).
- `was_sent(portfolio_id, analysis_run_id) -> bool`.

**Verify**: `uv run python -c "import sqlite3, app.repositories.db as d; c=sqlite3.connect(':memory:'); c.executescript(d._SCHEMA); print([r for r in c.execute(\"SELECT name FROM sqlite_master WHERE name='portfolio_recommendation_dispatches'\")])"`
→ one row.

### Step 2: Email template

New `app/agents/alert/templates/portfolio_recommendation.html`, table-based
inline-styled HTML matching the existing email system (Arial, 600px,
`#1e3a5f` headings, mobile stacking if the existing templates have it).
Structure per AC #442.3 and mockup surface 4:

1. Header: portfolio name + Strategy name (subject is built in the agent:
   `Recommendations — {portfolio_name} · {strategy_display_name} · {date}`).
2. Freshness banner when `result.freshness` is stale/missing/unknown (same
   prominent warning semantics as the screen, AC #442.4).
3. Market summary (reuse `_market_narrative.html` macro input).
4. Portfolio summary: total value, cash, unrealised P&L — scoped to this
   portfolio only (AC #442.5).
5. Actions in fixed order **Sell → Hold → Buy**, reusing
   `_sell_card.html`/`_buy_card.html` styling where possible; each row shows
   ticker, plain-language reason (from `RecommendationV1.reason`), rule code,
   evidence warnings.
6. Footer: provenance (strategy id, source digest, parameters, portfolio
   revision, run_id) + "No trades were placed." line.

Text body: plain-text equivalent of the same sections.

**Verify**: `uv run python -c "from app.agents.alert.templating import email_templates; email_templates.get_template('portfolio_recommendation.html'); print('ok')"`

### Step 3: `AlertAgent.send_portfolio_recommendation_email`

Sibling method on `AlertAgent` (do not touch `send_summary_email`):

- Input: the `RecommendationResultV1` + portfolio display name. It renders
  the template and sends via the existing SMTP helper; returns `True` on
  success, `False` on SMTP failure (logged, never raised into the pipeline).
- Subject identifies portfolio and Strategy (AC #442.3).
- No mutation of `_buy_alerts`/`_sell_alerts`; no recalculation of actions.

**Verify**: `uv run pytest tests/test_portfolio_recommendation_email.py -v`
(after Step 5; SMTP monkeypatched).

### Step 4: Dispatch orchestration

New `app/services/portfolio_recommendation_email_service.py`:

- `dispatch_all(run_id: str, market_narrative) -> DispatchSummary` —
  `list_assigned()` from plan 023; for each portfolio with an assignment:
  1. `claim(...)`; if already claimed/sent for this run → skip (idempotent).
  2. `recommend(portfolio_id)` (plan 024). Typed no-assignment/unavailable
     states → mark_failed + log + notification-centre event, continue.
  3. Build + send the email; `mark_sent` on success, `mark_failed` on
     failure. One portfolio's failure never prevents the others, pipeline
     completion policy, or the existing digest (AC #442.8).
- A stale-but-usable result is still sent, with its warning (AC #442.8).
- Clearing an assignment removes it from `list_assigned()` → no future sends;
  the consolidated digest is untouched (AC #442.6).

In `app/orchestration/orchestrator.py`, immediately **after** the existing
`alerter.send_summary_email(...)` call: wrap
`dispatch_all(run_id, market_narrative)` in a narrow try/except that logs and
records a notification on total failure — the pipeline must complete exactly
as before when dispatch explodes (AC #442.1, #442.8). Changing an assignment
never triggers an immediate send; the next successful daily scan uses the
then-current assignment (AC #442.9 — this falls out naturally: dispatch only
runs post-scan).

**Verify**: `uv run pytest tests/test_portfolio_recommendation_email.py -v`.

### Step 5: Tests

`tests/test_portfolio_recommendation_email.py` (SMTP monkeypatched; tmp_path
DBs; follow the `AlertAgent` test patterns that monkeypatch
`EMAIL_CONFIG`/`db_path`):

- multi-portfolio isolation: two assigned portfolios with disjoint holdings →
  each email contains only its own positions/values (AC #442.5); one failing
  send does not stop the other
- screen/email parity: the email's actions/groups/counts match
  `RecommendationResultV1` exactly (model on
  `tests/test_email_portfolio_parity.py`)
- template structure: subject contains portfolio + Strategy; sections appear
  in order market → portfolio → Sell → Hold → Buy; provenance footer present
- idempotent retry: `dispatch_all(run_id)` twice → exactly one send per
  portfolio; a new `run_id` sends again (AC #442.7)
- no-assignment suppression: unassigned portfolio gets no email and no
  receipt; clearing an assignment stops future sends (AC #442.6)
- stale warning: stale result still sent, banner present in HTML body
- failure isolation: recommendation evaluation failure for one portfolio →
  logged + `mark_failed`, other emails sent, digest unaffected
- existing daily-email tests: `uv run pytest
  tests/test_email_portfolio_parity.py -v` passes **unmodified**

### Step 6: Full suite, lint, format, types

**Verify**:
- `uv run pytest` → all pass
- `uv run ruff check app/ tests/` → `All checks passed!`
- `uv run ruff format app/ tests/` → unchanged (or reformat; re-stage only
  in-scope files)
- `uv run pyrefly check` → no new errors

## Test plan

See Step 5 — 8 test groups, all offline (monkeypatched SMTP, tmp_path DBs,
synthetic `RecommendationResultV1` fixtures). The existing consolidated-email
suite is the regression net for "digest unchanged".

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `portfolio_recommendation_dispatches` table present; PK is
      `(portfolio_id, analysis_run_id)`
- [ ] `uv run pytest tests/test_portfolio_recommendation_email.py -v` → all
      pass
- [ ] `uv run pytest tests/test_email_portfolio_parity.py -v` → all pass
      with **zero modifications** to those tests
- [ ] `uv run pytest` exits 0
- [ ] `uv run ruff check app/ tests/` → `All checks passed!`
- [ ] `uv run pyrefly check` → no new errors
- [ ] Grep guard: `send_summary_email` body unchanged
      (`git diff c12ef330..HEAD -- app/agents/alert/alert_agent.py` shows only
      the added sibling method)
- [ ] Grep guard: no action-rule logic (`exit_signal`/`entry_signal`
      evaluation) in email template or send code
- [ ] `git status` shows only in-scope files modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Plans 023/024 are not DONE, or `RecommendationResultV1` /
  `list_assigned()` don't exist with the documented shapes.
- The orchestrator's digest call site moved or its surrounding
  failure-handling policy changed such that a post-digest hook can't be added
  narrowly — report the new shape; don't widen the try/except over the digest
  itself.
- `send_summary_email`'s SMTP helper is not reusable as-is (e.g. it closes
  over digest-specific state) — extract minimally and report, rather than
  duplicating SMTP code.
- Existing daily-email tests fail for reasons unrelated to this plan — report;
  do not modify them to make this plan pass.

## Maintenance notes

- The receipt table is the only send-authority; never send based on
  "today's file wasn't sent" heuristics.
- `DispatchSummary` (sent / failed / skipped counts) should be recorded to the
  notification centre so the UI can surface dispatch health later.
- Per-portfolio recipients and intraday sends are explicitly out of scope
  (feature #439 decisions); the receipt PK already generalizes if a future
  plan adds run-scoped triggers beyond the daily scan.
