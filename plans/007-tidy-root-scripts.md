# Plan 007: Tidy root-dir one-off scripts and ignore temp debug files

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan in
> `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git status --short && git ls-files check_results.py patch_cash_history.py regen_excel.py`
> If the three tracked scripts are no longer tracked, or the untracked `tmp_*`
> files listed below are gone, the working tree has drifted — re-read the
> "Current state" section before proceeding.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW (moving unreferenced scripts + adding `.gitignore` rules; no
  application code imports these)
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `ce96c93`, 2026-06-18

## Why this matters

The repo root has accumulated one-off and throwaway scripts that obscure the
real entry points (`orchestrator.py`, `models.py`, `web/app.py`). Three are
**tracked** in git (`check_results.py`, `patch_cash_history.py`,
`regen_excel.py`) and several `tmp_*.py` debug scripts sit **untracked**,
showing up in every `git status` as noise. This is cosmetic — LOW impact, no
correctness effect — but it is cheap to fix and makes the project legible:
move the tracked one-offs into the existing `scripts/` directory (where
`migrate_portfolio_col.py`, `validate_specs.py`, etc. already live), and add
`.gitignore` rules so the `tmp_*` scratch files stop appearing as untracked.

This is the lowest-value plan in the set; it is included because it was
explicitly requested. Skip it without guilt if priorities shift.

## Current state

A `scripts/` directory already exists and is the established home for utility
scripts:

```
scripts/
  import_brokerage_trades.py
  merge_brokerage_history.py
  migrate_portfolio_col.py
  validate_specs.py
```

Three **tracked** one-off scripts clutter the root (confirmed tracked via
`git ls-files`):
- `check_results.py`
- `patch_cash_history.py`
- `regen_excel.py`

Confirmed: **no module imports any of these** (verified with
`grep -rn "import check_results\|import patch_cash_history\|import regen_excel"`
→ no matches), so moving them cannot break an import.

Several **untracked** `tmp_*` scratch files also sit in the root:
- `tmp_check_holdings.py`
- `tmp_debug_backfill.py`
- `tmp_price_cache.py`

`.gitignore` already ignores some debug scripts via `**/` rules (e.g.
`**/tmp_debug.py`, `**/debug_positions.py`, `tmp_import_check.py`) but **not**
the three `tmp_*` files above. The relevant existing block:

```gitignore
# .gitignore (existing block)
# Intermediate/debug scripts and notebooks (may contain portfolio details)
**/import_brokerage_trades.py
...
**/tmp_debug.py
```

### Repo conventions to match
- Utility/maintenance scripts live in `scripts/`.
- The `.gitignore` groups debug/scratch files under the
  "Intermediate/debug scripts" comment block — add new rules there.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Confirm no imports | `grep -rn "import check_results\|import patch_cash_history\|import regen_excel\|from check_results\|from patch_cash_history\|from regen_excel" --include="*.py" .` | no matches |
| Confirm move (tracked) | `git ls-files scripts/check_results.py scripts/patch_cash_history.py scripts/regen_excel.py` | 3 paths listed |
| Confirm tmp ignored | `git status --short` | none of the `tmp_*` files appear |
| Sanity: nothing else broke | `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` | 0 failed |

## Scope

**In scope** (the only files you may modify/move):
- `check_results.py`, `patch_cash_history.py`, `regen_excel.py` → `git mv` into
  `scripts/`.
- `.gitignore` (add rules for the three untracked `tmp_*` files).

**Out of scope** (do NOT touch):
- Any script already gitignored (`backfill_*.py`, `debug_positions.py`,
  `query_alert_history.py`, etc.) — leave them where they are.
- `orchestrator.py`, `models.py`, `ms_agent_framework.py`, `run_tests.py` —
  these are real entry points / referenced modules; they stay in root.
- The contents of the moved scripts — move only, do not edit.
- Deleting the `tmp_*` files — only ignore them; the user may still want them
  locally.

## Git workflow

- Branch: `advisor/007-tidy-root-scripts`
- Two commits: one for the `git mv`, one for the `.gitignore` change
  (e.g. `chore: move one-off scripts into scripts/` and
  `chore(gitignore): ignore tmp_* scratch scripts`).
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Re-confirm nothing imports the three scripts

**Verify**:
`grep -rn "import check_results\|import patch_cash_history\|import regen_excel\|from check_results\|from patch_cash_history\|from regen_excel" --include="*.py" . | grep -v ".spec-gen"`
→ no matches. If there IS a match, STOP (see STOP conditions).

### Step 2: Move the three tracked scripts into `scripts/`

```bash
git mv check_results.py scripts/check_results.py
git mv patch_cash_history.py scripts/patch_cash_history.py
git mv regen_excel.py scripts/regen_excel.py
```

**Verify**: `git ls-files scripts/check_results.py scripts/patch_cash_history.py scripts/regen_excel.py` → 3 paths listed; `ls check_results.py 2>/dev/null` → not found.

### Step 3: Ignore the untracked `tmp_*` scratch files

In `.gitignore`, under the existing
`# Intermediate/debug scripts and notebooks` block, add:

```gitignore
**/tmp_check_holdings.py
**/tmp_debug_backfill.py
**/tmp_price_cache.py
```

(Or, if the user prefers a blanket rule, `tmp_*.py` would cover all current and
future scratch files — but note `tmp_import_check.py` is already listed
explicitly, so a blanket `tmp_*.py` would make that line redundant. Prefer the
three explicit lines to match the existing per-file style unless told otherwise.)

**Verify**: `git status --short` → none of `tmp_check_holdings.py`,
`tmp_debug_backfill.py`, `tmp_price_cache.py` appear in the output.

### Step 4: Sanity-check nothing broke

Moving unreferenced scripts should not affect anything, but confirm:

**Verify**: `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` → 0 failed.

## Test plan

No new tests — this is a file-organization change with no runtime behavior. The
existing suite passing is the only regression gate needed (it confirms the move
didn't break an import path that a static grep missed).

## Done criteria

ALL must hold:

- [ ] `git ls-files scripts/check_results.py scripts/patch_cash_history.py scripts/regen_excel.py` → 3 paths.
- [ ] `ls check_results.py patch_cash_history.py regen_excel.py 2>/dev/null` → nothing (all moved).
- [ ] `git status --short` shows none of the three `tmp_*` files.
- [ ] `python -m pytest tests/ -o addopts="" -q -p no:cacheprovider` exits 0.
- [ ] No files outside the in-scope list modified or moved (`git status`).
- [ ] `plans/README.md` status row for 007 updated to DONE.

## STOP conditions

Stop and report back (do not improvise) if:

- Step 1 finds a real import of any of the three scripts (something references
  them — moving would break it; report what references them).
- A moved script is referenced by a non-Python caller (a `.md` doc, a CI file,
  a `.bat`/`.ps1`) — `grep -rn "check_results\|patch_cash_history\|regen_excel"
  --include="*.md" --include="*.yml" --include="*.yaml" --include="*.ps1" .`
  before finishing; if a runnable reference exists, report it.
- The test suite fails after the move (implies a hidden dependency).

## Maintenance notes

- After this lands, the root holds only real entry points and tracked config —
  new one-off scripts should go straight into `scripts/`, and scratch files
  should be named `tmp_*` so the gitignore rule catches them.
- If the team later wants a single blanket `tmp_*.py` ignore rule, it can
  replace the per-file lines (and the older `tmp_import_check.py` /
  `**/tmp_debug.py` entries) — a small follow-up cleanup, intentionally not done
  here to keep the diff minimal.
