---
title: 'Use consistent run terminology in Strategy Manager'
type: 'enhancement'
status: 'done'
github_issue: 415
baseline_revision: 'e33961f2'
---

## Tasks & Acceptance

- [x] Replace user-visible Strategy Manager references to an “attempt” or an “activity” with the appropriate “run” wording.
- [x] Make the backtest history identify entries as runs and label an active readiness state “Running”.
- [x] Preserve internal route paths, model names, polling identifiers, lifecycle semantics, and durable audit data.
- [x] Add focused route/template assertions for the new copy and run the focused suite.

**Acceptance Criteria:**

- A user sees “Run” (including run controls, history, cancellation, and deletion copy) rather than “Attempt” or “Activity” in Strategy Manager UI.
- A running state is displayed as “Running”; queued, terminal, and cancellation behaviours remain unchanged.
- Existing `/strategy-manager/activities/...` routes and htmx polling contracts continue to work unchanged.

## Recorded decision

“Run” is a presentation term only. Existing route URLs, Python symbols, database values, and background-job contracts intentionally retain their established activity/attempt terminology to avoid an unnecessary compatibility migration.

## Verification

- `/Users/me/Git/Agents.stocks/.venv/bin/python -m pytest tests/test_strategy_manager_routes.py tests/test_notifications_route.py tests/backtest/test_strategy_job_recovery.py -q` — 186 passed.
- `/Users/me/Git/Agents.stocks/.venv/bin/ruff check app/api/routes/strategy_manager.py app/services/backtest/notification_projector.py tests/test_strategy_manager_routes.py tests/test_notifications_route.py tests/backtest/test_strategy_job_recovery.py` — passed.
- `git diff --check` — passed.
