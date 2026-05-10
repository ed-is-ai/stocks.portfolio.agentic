## 1. Promote specs to permanent location

- [x] 1.1 Move system-architecture/spec.md to openspec/specs/system-architecture/
- [x] 1.2 Move data-models/spec.md to openspec/specs/data-models/
- [x] 1.3 Move scanner-agent/spec.md to openspec/specs/scanner-agent/
- [x] 1.4 Move analyst-agent/spec.md to openspec/specs/analyst-agent/
- [x] 1.5 Move alert-agent/spec.md to openspec/specs/alert-agent/
- [x] 1.6 Move trader-agent/spec.md to openspec/specs/trader-agent/
- [x] 1.7 Move extraction-agent/spec.md to openspec/specs/extraction-agent/

## 2. Create extension guides and how-to documents

- [x] 2.1 Create openspec/specs/scanner-agent/extension-guide.md with step-by-step: "How to add a new data source"
- [x] 2.2 Create openspec/specs/analyst-agent/extension-guide.md with step-by-step: "How to add new scoring framework"
- [x] 2.3 Create openspec/specs/alert-agent/extension-guide.md with step-by-step: "How to add new alert channel"
- [x] 2.4 Create openspec/specs/trader-agent/extension-guide.md with step-by-step: "How to integrate new broker"
- [x] 2.5 Create openspec/specs/extraction-agent/extension-guide.md with step-by-step: "How to add new watchlist source"

## 3. Cross-reference specs from codebase

- [x] 3.1 Add docstring reference to agents/scanner/scanner_agent.py: "See openspec/specs/scanner-agent/"
- [x] 3.2 Add docstring reference to agents/analyst/analyst_agent.py: "See openspec/specs/analyst-agent/"
- [x] 3.3 Add docstring reference to agents/alert/alert_agent.py: "See openspec/specs/alert-agent/"
- [x] 3.4 Add docstring reference to agents/trader/trader_agent.py: "See openspec/specs/trader-agent/"
- [x] 3.5 Add docstring reference to agents/extraction/extraction_agent.py: "See openspec/specs/extraction-agent/"
- [x] 3.6 Update README.md with link to openspec/specs/system-architecture/ for onboarding

## 4. Set up spec maintenance workflow

- [x] 4.1 Create SPEC_MAINTENANCE.md documenting when/how to update specs (during feature planning, before major refactors)
- [x] 4.2 Update CLAUDE.md to reference specs for new developers joining the project
- [x] 4.3 Add CI check: verify specs/*/spec.md files are well-formed (OpenSpec validation)

## 5. Create onboarding materials using specs

- [x] 5.1 Create ONBOARDING.md that walks new developer through specs in sequence: system-architecture → data-models → one agent spec
- [x] 5.2 Create ARCHITECTURE.md as human-readable summary of system design (can reference specs for details)
- [x] 5.3 Create EXTENSION_PATTERNS.md collecting all extension guides in one place (index to individual guides)

## 6. Validate specs against existing code

- [x] 6.1 Audit Scanner Agent code against scanner-agent/spec.md (verify all technical indicators listed are actually computed)
- [x] 6.2 Audit Analyst Agent code against analyst-agent/spec.md (verify CANSLIM scoring implemented as specified)
- [x] 6.3 Audit Alert Agent code against alert-agent/spec.md (verify 24h cooldown logic matches spec)
- [x] 6.4 Audit Trader Agent code against trader-agent/spec.md (verify risk limits and position sizing match spec)
- [x] 6.5 Audit Extraction Agent code against extraction-agent/spec.md (verify source deduplication and tagging)
- [x] 6.6 Document any discrepancies found as TODO items for future fixes (don't modify specs retroactively unless bugs found)

## 7. Gather team feedback and iterate

- [ ] 7.1 Share specs with team: "Read openspec/specs/ and flag any gaps or inaccuracies"
- [ ] 7.2 Collect feedback: missing detail, unclear wording, incomplete extension points
- [ ] 7.3 Update specs based on team feedback (use openspec propose/apply for iterations)
- [ ] 7.4 Lock specs version 1: commit to main branch when team confirms accuracy
