# Spec Maintenance Guide

Keeping specifications accurate and current is essential for onboarding, planning, and refactoring. This guide establishes when and how to update specs.

## Principle: Specs Are Load-Bearing

Specifications in `openspec/specs/` are the **source of truth** for:
- How agents work (onboarding new team members)
- How to extend agents (adding new features)
- What can't change without coordinating (architecture constraints)

When code behavior changes, specs must update. When specs are wrong, they mislead developers and cause bugs.

---

## When to Update Specs

### 1. Before Starting New Feature Work

**Trigger**: You're planning to add a new capability (data source, scoring framework, alert channel, broker, watchlist source).

**Action**:
1. Read the agent's **extension guide** (e.g., [Scanner Extension Guide](openspec/specs/scanner-agent/extension-guide.md))
2. Read the agent's **spec** (e.g., [Scanner Spec](openspec/specs/scanner-agent/spec.md))
3. If extension guide matches your use case: follow it (no spec update needed)
4. If extension guide doesn't quite fit: **open an issue or discuss with team** before proceeding
5. If you're adding a new type of extension not covered: **update extension guide** to document your pattern

**Example**:
- Planning to add Finviz as watchlist source? Extension guide covers it → no spec update needed
- Planning to add a custom data-cleaning step before Scanner runs? Not covered → discuss with team, update spec if approved

### 2. When Implementing Breaking Changes

**Trigger**: Your feature changes how an agent works or outputs data in a way that affects downstream agents.

**Examples of Breaking Changes**:
- Changing StockRecord schema (adding/removing/renaming fields)
- Changing StockAnalysis output format
- Changing recommended_action values (BUY, SELL, etc.)
- Changing a scoring algorithm fundamentally
- Changing pipeline execution order

**Action**:
1. **Before coding**: Create OpenSpec change proposal documenting the change
   ```bash
   openspec new change "fix-<issue>-description"
   ```
2. **In proposal.md**: Describe what's changing and why
3. **In design.md**: Explain the new behavior and migration plan
4. **In specs**: Update affected agent specs with new requirements
5. **In tasks.md**: Document breaking change impacts (what else needs updating)
6. **Review with team**: Get approval before implementing
7. **Implement**: Code changes following spec updates

### 3. When Fixing Bugs Found During Audits

**Trigger**: During code audits (Group 6 tasks), you find discrepancies between spec and code.

**Action**:
1. **Assess**: Is the spec wrong or the code wrong?
   - Spec is wrong: Update spec, no code change needed
   - Code is wrong: Fix code, update spec to match reality
   - Both wrong: Clarify intent with team, fix both

2. **Document**: Add TODO comment in code if fixing deferred
   ```python
   # TODO: Field 'x' spec says behavior is Y but code implements Z. 
   # See SPEC_MAINTENANCE.md audit notes for context.
   ```

3. **Update**: Fix whichever is wrong (spec or code)

4. **Test**: Verify fix doesn't break downstream agents

**Example**:
- Spec says "Scanner computes SMA200 from 252 days of data"
- Code only has 200 days available for new IPO
- Fix: Update spec to clarify "SMA200 computed if 200+ days available, else None"

### 4. When Refactoring Code (Non-Breaking)

**Trigger**: You're refactoring without changing external behavior (e.g., extracting helper functions, renaming internal variables, reorganizing code).

**Action**: No spec update needed. Specs document *what* agents do, not *how* they do it internally.

**Exception**: If refactoring changes dependencies or constraints, update spec.
- Example: If Scanner previously relied on global cache but you remove it, update spec to note "no caching" change.

### 5. When Updating Extension Guides

**Trigger**: You complete an extension and realize the guide is missing steps or has issues.

**Action**:
1. Update the extension guide file (e.g., `openspec/specs/scanner-agent/extension-guide.md`)
2. Add any missing steps or clarifications
3. Note what made implementation harder than guide suggested
4. Update checklist if new tasks needed

**Example**:
- Extension guide says "5 steps" but your implementation took 7
- Add missing steps to guide
- Add new pitfall you discovered to "Common Pitfalls" section

---

## How to Update Specs

### Option 1: Small Clarifications (Single Agent)

For minor clarifications (wording, examples, removing ambiguity) that don't change behavior:

```bash
# Edit spec directly
# Don't need formal OpenSpec process for pure documentation fixes
vi openspec/specs/<agent>/spec.md
git commit -m "docs(<agent>): clarify requirement description"
git push
```

**Example**:
- Clarifying what a field means
- Adding an example to a requirement
- Fixing a typo
- Improving wording for clarity

### Option 2: Requirement Changes (Formal OpenSpec)

For changes to *what* agents do (new requirements, modified behavior):

```bash
# Use OpenSpec to propose and track changes
openspec new change "update-<agent>-behavior"

# Edit proposal, design, specs, tasks using openspec instructions
openspec instructions proposal --change "update-<agent>-behavior" --json

# Follow the OpenSpec workflow
```

**Example**:
- Adding a new field to StockRecord
- Changing how CANSLIM score is weighted
- Adding new alert channel type
- Modifying risk limit constraints

### Option 3: Extension Guide Updates

Edit extension guides directly:

```bash
vi openspec/specs/<agent>/extension-guide.md
git commit -m "docs(<agent>): update extension guide with missing steps"
git push
```

**Example**:
- Adding code example
- Documenting new pitfall
- Clarifying step sequence
- Adding test strategy

---

## Spec Update Checklist

When updating a spec, verify:

- [ ] **Requirement is Clear**: Can a new developer understand what to do?
- [ ] **Scenarios Exist**: Each requirement has at least one "WHEN...THEN" scenario
- [ ] **Field Types Match Code**: StockRecord fields match models.py
- [ ] **Downstream Impact Documented**: If spec changes affect other agents, are they noted?
- [ ] **Extension Guide Updated**: Does extension guide still match spec?
- [ ] **Architecture Constraints Updated**: Any new constraints documented?
- [ ] **Examples Provided**: Complex requirements have concrete examples
- [ ] **No Ambiguity**: Multiple interpretations possible? Clarify.

---

## Review & Approval

### For Breaking Changes

Breaking changes require **team approval** before implementation:

1. **Create proposal** (via OpenSpec)
2. **Present to team** (in meeting or async review)
3. **Document impacts** (what else breaks, migration plan)
4. **Get approval** (team confirms it's the right approach)
5. **Implement** (code changes)
6. **Update all affected specs** (not just the primary agent)

**Example**:
- Changing StockRecord.price_history order (oldest→newest vs. newest→oldest)
- Impacts: Scanner (produces it), Analyst (consumes it) — both specs need updating
- Migration: Need to handle both orders during transition or migrate all existing data

### For Non-Breaking Changes

Non-breaking changes (new optional fields, new extension type) can be:
- Implemented and documented in pull request
- Reviewed by team for correctness
- Merged once tests pass

**Example**:
- Adding optional new field to StockRecord
- Adding new scoring framework
- Adding new alert channel

---

## Spec Review Process

### Code Review Checklist for Spec Changes

When reviewing PRs that change specs:

- [ ] Spec change matches code change (are they in sync?)
- [ ] Requirement is testable (can QA verify it?)
- [ ] Examples are concrete (not abstract)
- [ ] No ambiguity in language
- [ ] Architecture constraints still valid
- [ ] Extension guide updated (if applicable)
- [ ] Downstream impacts acknowledged
- [ ] Backward compatibility considered (or breaking change documented)

### Spec Audits

Periodically (quarterly or before major release):

1. **Pick an agent**: Read its spec
2. **Audit the code**: Does code match spec?
3. **Document findings**: Discrepancies, missing details, outdated constraints
4. **Prioritize fixes**: Critical bugs vs. documentation-only improvements
5. **Update spec or code**: Fix whichever is wrong
6. **Test**: Ensure fix doesn't break downstream agents

**Audit Template**:
```markdown
## Scanner Agent Audit (Q1 2025)

### Findings
- [ ] Price history order: Spec says oldest→newest, code implements newest→oldest ❌
- [ ] SMA200: Spec says always computed, code returns None if <200 days ❌
- [ ] API errors: Spec says graceful degradation, code crashes on timeout ❌
- [ ] Documentation: Spec examples are clear and accurate ✓

### Action Items
1. Fix code to honor price_history order specification
2. Update spec: "SMA200 computed if 200+ days available, else None"
3. Add backoff/retry logic for API timeouts
```

---

## Keeping Extension Guides Current

Extension guides should be updated when:

1. **You complete an extension**: Did the guide work? What was missing?
2. **You discover a pitfall**: Add to "Common Pitfalls" section
3. **API changes**: If external APIs used by extension change, update guide
4. **New patterns emerge**: If different approach is better, update guide

**Example Update**:
```markdown
## Common Pitfalls

**Pitfall:** API rate limits cause 429 responses
→ Mitigation: Add backoff/retry logic (NEW)
```

---

## Automation & CI Checks

### Spec Validation

CI/CD should verify specs are well-formed:

```bash
# Validate spec structure (YAML/JSON format, required fields)
openspec validate openspec/specs/*/spec.md

# Lint specs (check for common issues)
openspec lint openspec/specs/
```

**What to check**:
- All requirements have scenarios (#### Scenario: format)
- Field types are valid (string, int, float, bool)
- No undefined references (e.g., field names not in models)
- Extension guides exist for agents that have them

### Code-Spec Consistency Checks

Consider adding checks that:

```python
# Verify StockRecord fields match spec
def test_stock_record_fields_match_spec():
    fields = set(StockRecord.model_fields.keys())
    # Compare against spec's documented fields
    # Fail if mismatch (new field added but not documented)
```

This catches:
- New fields added to models without updating spec
- Fields removed without updating spec
- Field type changes

---

## Documentation Updates

When updating specs, also consider:

1. **README.md**: Does quick start still make sense?
2. **ONBOARDING.md**: Does onboarding guide still point to right specs?
3. **ARCHITECTURE.md**: Does system architecture description need updating?
4. **EXTENSION_PATTERNS.md**: Do patterns still apply?
5. **Agent docstrings**: Do module docstrings reference correct spec location?

---

## FAQ

**Q: Can I update code without updating specs?**  
A: Only if behavior doesn't change (refactoring, bug fixes that make code match existing spec). If behavior changes, update spec first or simultaneously.

**Q: What if I disagree with the spec?**  
A: Discuss with team. Specs capture *intended* behavior; if spec is wrong, fix it. If you think the intended behavior should change, make the case and update specs.

**Q: How often should I audit specs?**  
A: After major features (quarterly or with each release). Quick spot-checks during code reviews catch most issues.

**Q: Can specs go out of sync with code?**  
A: Yes, and it's a problem. Code reviews should catch this. Quarterly audits help find drifts.

**Q: What if a spec is too vague?**  
A: Update it! Examples, constraints, and scenarios reduce ambiguity. If you had to guess what the spec means, others will too.

---

## Tools & Resources

- **OpenSpec CLI**: `openspec new change`, `openspec instructions`, `openspec apply`
- **Validation**: `openspec validate openspec/specs/`
- **Extension Guides**: `openspec/specs/<agent>/extension-guide.md`
- **Code**: Compare `openspec/specs/` against actual code in `agents/`
- **Reviews**: PR review checklist above

---

## Summary

**Specs are not "write once".**  They evolve as:
- New extensions are added
- Bugs are found and fixed
- Team learns better patterns
- Architecture matures

**Update specs when**:
- Starting new feature (read guide first)
- Making breaking changes (propose → approve → code)
- Fixing bugs (spec or code, whichever is wrong)
- Learning new patterns (update extension guide)

**Keep specs in sync**: Code reviews + quarterly audits catch drift early.

**Specs enable**: Onboarding, planning, refactoring with confidence.

---

## Contacts & Questions

- **Spec questions**: Read the spec for your agent, then ask team lead
- **Found a bug**: Document in spec's TODO, then fix
- **Want to suggest improvement**: Update extension guide or create issue
- **Unclear requirement**: That's a spec problem—clarify and update
