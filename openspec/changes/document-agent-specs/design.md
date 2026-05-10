## Context

The Agents.Stocks system is a production stock portfolio management platform with 5 autonomous agents (Scanner, Analyst, Alert, Trader, Extraction) orchestrated by a central Orchestrator. Each agent has distinct responsibilities and data contracts. The codebase uses Pydantic models, MS Agent framework, and integrates with multiple external APIs (yfinance, FMP, Alpha Vantage, Congress API). Documentation currently exists only as inline docstrings and git commit messages.

## Goals / Non-Goals

**Goals:**
- Create formal capability specifications for each agent covering purpose, inputs/outputs, design decisions
- Document extension points for future feature development (new data sources, scoring methods, alert channels)
- Establish architecture constraints and dependency graph to enable confident refactoring
- Provide onboarding clarity: one central place to understand system design and agent roles
- Create specifications that guide "how to add X" for common extension scenarios

**Non-Goals:**
- Refactor or change existing code (documentation-only)
- Create API documentation (reference docs belong elsewhere)
- Document every function and method (focus on system design, not line-by-line)
- Establish CI/CD pipeline or deployment procedures
- Create performance benchmarks or SLAs

## Decisions

### 1. Spec Organization Structure
**Decision**: Use one `specs/<capability>/spec.md` per agent plus separate specs for system-architecture and data-models.

**Rationale**: 
- Each agent is independently deployable and has distinct lifecycle
- Extension points are agent-specific (adding a data source goes to scanner-agent, not analyst-agent)
- Centralizing system design in system-architecture prevents duplication
- Data models are shared contracts that multiple agents depend on

**Alternatives Considered**:
- Monolithic spec (rejected: too large, mixing concerns, hard to maintain)
- One spec per file (rejected: excessive fragmentation, obscures dependencies)

### 2. Spec Content Structure
**Decision**: Each spec includes: Purpose, Inputs/Outputs, Design Decisions, Extension Points, Constraints, Known Issues, and Dependencies.

**Rationale**:
- Purpose + Inputs/Outputs serve onboarding (What does this do? What does it consume/produce?)
- Design Decisions explain why current implementation chosen (refactoring context)
- Extension Points guide future work (How do I add a new X?)
- Constraints document load-bearing assumptions (What breaks if I change this?)
- Known Issues capture tech debt and planned work (What's not perfect?)

**Alternatives Considered**:
- Minimalist specs (rejected: too opaque for refactoring and planning)
- Separate extension guide (rejected: puts guidance far from specs, harder to maintain)

### 3. Data Model Specification
**Decision**: Create shared `data-models/spec.md` documenting all Pydantic models with field semantics and validation rules.

**Rationale**:
- Data models are the contract between agents
- Field meanings (e.g., `price_history` is oldest→newest, `rel_volume` is decimal) are critical for correct implementation
- Validation rules and optional fields affect how downstream code uses models
- Centralized avoids duplication across agent specs

### 4. Extension Points Pattern
**Decision**: Document extension points as "To add X, follow this pattern" with concrete code references.

**Rationale**:
- Developers learn by example, not abstract principles
- Pointing to existing implementations (e.g., "new data source: see congress_client.py") reduces onboarding friction
- Makes it clear what is "extension-point stable" vs. internal implementation

### 5. Constraint Documentation
**Decision**: For each agent, explicitly list what cannot change without breaking the system (API contracts, database schema dependencies, external API dependencies).

**Rationale**:
- Refactoring decisions need clear boundaries
- Prevents accidental breaking changes (e.g., changing StockRecord schema or agent output format)
- Documents implicit assumptions (e.g., Scanner always produces StockRecord, not just dict)

## Risks / Trade-offs

[Risk] Specs become stale if not maintained alongside code changes
→ Mitigation: Establish discipline to update specs during feature planning PRs; OpenSpec change proposals help keep docs in sync with code.

[Risk] Extension points may not cover all use cases
→ Mitigation: Extension points document patterns for common extensions; unusual cases require design discussions and spec updates.

[Risk] Too much detail makes specs hard to read
→ Mitigation: Use progressive disclosure—start with Purpose and Inputs/Outputs for onboarding, let refactoring and planning dig into Constraints and Decisions as needed.

[Risk] System architecture spec becomes a bottleneck for unrelated changes
→ Mitigation: Architecture spec documents orchestration flow and data contracts only; agent implementations can change freely if contracts are honored.

## Open Questions

1. **Agent State & Persistence**: Should specs document how agents maintain state (scan_history.db, alerts.db)? Or is that implementation detail?
   - Proposed: Document briefly (what state is kept, why) but not schema details.

2. **Error Handling Strategy**: Do specs need to document error paths? (e.g., "if yfinance fails, Scanner does X")
   - Proposed: Document only if error handling affects contracts or system stability (e.g., graceful degradation).

3. **Configuration & Tuning**: Should specs document configurable parameters (alert thresholds, cooldown hours)?
   - Proposed: Yes, with rationale for defaults; parameters that change behavior belong in specs.
