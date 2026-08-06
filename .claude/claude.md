# Development Guidelines

This document contains critical information about working with this codebase. Follow these guidelines precisely.

## Core Development Rules

1. Package Management
   - ONLY use uv, NEVER pip
   - Installation: `uv add package`
   - Running tools: `uv run tool`
   - Upgrading: `uv add --dev package --upgrade-package package`
   - FORBIDDEN: `uv pip install`, `@latest` syntax

2. Code Quality
   - Type hints required for all code
   - use pyrefly for type checking
     - run `pyrefly init` to start
     - run `pyrefly check` after every change and fix resultings errors
   - Public APIs must have docstrings
   - Functions must be focused and small
   - Follow existing patterns exactly
   - Line length: 88 chars maximum

3. Testing Requirements
   - Framework: `uv run pytest`
   - Async testing: use anyio, not asyncio
   - Coverage: test edge cases and errors
   - New features require tests
   - Bug fixes require regression tests

4. Code Style
    - PEP 8 naming (snake_case for functions/variables)
    - Class names in PascalCase
    - Constants in UPPER_SNAKE_CASE
    - Document with docstrings
    - Use f-strings for formatting

## Development Philosophy

- **Simplicity**: Write simple, straightforward code
- **Readability**: Make code easy to understand
- **Performance**: Consider performance without sacrificing readability
- **Maintainability**: Write code that's easy to update
- **Testability**: Ensure code is testable
- **Reusability**: Create reusable components and functions
- **Less Code = Less Debt**: Minimize code footprint

## Coding Best Practices

- **Early Returns**: Use to avoid nested conditions
- **Descriptive Names**: Use clear variable/function names (prefix handlers with "handle")
- **Constants Over Functions**: Use constants where possible
- **DRY Code**: Don't repeat yourself
- **Functional Style**: Prefer functional, immutable approaches when not verbose
- **Minimal Changes**: Only modify code related to the task at hand
- **Function Ordering**: Define composing functions before their components
- **TODO Comments**: Mark issues in existing code with "TODO:" prefix
- **Simplicity**: Prioritize simplicity and readability over clever solutions
- **Build Iteratively** Start with minimal functionality and verify it works before adding complexity
- **Run Tests**: Test your code frequently with realistic inputs and validate outputs
- **Build Test Environments**: Create testing environments for components that are difficult to validate directly
- **Functional Code**: Use functional and stateless approaches where they improve clarity
- **Clean logic**: Keep core logic clean and push implementation details to the edges
- **File Organsiation**: Balance file organization with simplicity - use an appropriate number of files for the project scale

## System Architecture

- use pydantic and MS agent framework
- this project is a simple stock portfilio manager. Keep files for seaprate agents, orchestration



## Pull Requests

- Create a detailed message of what changed. Focus on the high level description of
  the problem it tries to solve, and how it is solved. Don't go into the specifics of the
  code unless it adds clarity.

## Git Workflow

- Always use feature branches; do not commit directly to `main`
  - Name branches descriptively: `fix/auth-timeout`, `feat/api-pagination`, `chore/ruff-fixes`
  - Keep one logical change per branch to simplify review and rollback
- Create pull requests for all changes
  - Open a draft PR early for visibility; convert to ready when complete
  - Ensure tests pass locally before marking ready for review
  - Use PRs to trigger CI/CD and enable async reviews
- Link issues
  - Before starting, reference an existing issue or create one
  - Use commit/PR messages like `Fixes #123` for auto-linking and closure
- Commit practices
  - Make atomic commits (one logical change per commit)
  - Prefer conventional commit style: `type(scope): short description`
    - Examples: `feat(eval): group OBS logs per test`, `fix(cli): handle missing API key`
  - Squash only when merging to `main`; keep granular history on the feature branch
- Practical workflow
  1. Create or reference an issue
  2. `git checkout -b feat/issue-123-description`
  3. Commit in small, logical increments
  4. `git push` and open a draft PR early
  5. Convert to ready PR when functionally complete and tests pass
  6. Merge after reviews and checks pass

## Python Tools

- use context7 mcp to check details of libraries

## Code Formatting

1. Ruff
   - Format: `uv run ruff format .`
   - Check: `uv run ruff check .`
   - Fix: `uv run ruff check . --fix`
   - Critical issues:
     - Line length (88 chars)
     - Import sorting (I001)
     - Unused imports
   - Line wrapping:
     - Strings: use parentheses
     - Function calls: multi-line with proper indent
     - Imports: split into multiple lines

2. Type Checking
  - run `pyrefly init` to start
  - run `pyrefly check` after every change and fix resultings errors
   - Requirements:
     - Explicit None checks for Optional
     - Type narrowing for strings
     - Version warnings can be ignored if checks pass


## Error Resolution

1. CI Failures
   - Fix order:
     1. Formatting
     2. Type errors
     3. Linting
   - Type errors:
     - Get full line context
     - Check Optional types
     - Add type narrowing
     - Verify function signatures

2. Common Issues
   - Line length:
     - Break strings with parentheses
     - Multi-line function calls
     - Split imports
   - Types:
     - Add None checks
     - Narrow string types
     - Match existing patterns

3. Best Practices
   - Check git status before commits
   - Run formatters before type checks
   - Keep changes minimal
   - Follow existing patterns
   - Document public APIs
   - Test thoroughly

## Quarterly SIPP Portfolio Update

The portfolio is maintained through quarterly SIPP (Self-Invested Personal Pension) CSV imports. The process is automated in `TraderAgent.import_sipp()`.

### Process

1. **Export SIPP CSV** from your provider (interactive investor, AJ Bell, etc.)
   - File format: Date, Symbol, Sedol, Quantity, Price, Description, Reference, Debit, Credit, Running Balance
   - Recommended filename: `merged.csv` in `data/processed/SIPP/`

2. **Run the import** (typically done via portfolio tab in web UI):
   ```python
   from agents.trader.trader_agent import TraderAgent
   agent = TraderAgent()
   cash_balance = agent.import_sipp('data/processed/SIPP/merged.csv')
   ```

3. **Verify results**:
   - Check portfolio count (should match actual holdings)
   - Confirm cash balance matches account statement
   - Review open positions and average cost basis

### Key Import Logic

- **Trades**: Only rows with valid Symbol field (not 'n/a') are imported as stock trades
- **Exception**: HSBC GLOB funds use description matching; ticker is 'HSFWA'
- **Cash flows**: Non-trade entries (contributions, tax relief, interest, dividends) stored separately in cash_flows table
- **Date sorting**: Trades replayed chronologically (DD/MM/YYYY converted to YYYY/MM/DD for sorting)
- **Cash position**: Running Balance of the latest-dated row is the authoritative cash balance, chosen independently of CSV row order (a newest-first export yields the same balance as oldest-first) (#158)

### Common Issues

**Too many open positions (>10)?**
- Check CSV for duplicate Symbol entries with different Sedol values
- Ensure Symbol column is filled for all valid trades (not 'n/a')
- Run with sorted CSV (chronological order, oldest first)

**Negative shares in position?**
- Indicates sells exceed buys for that ticker
- Check CSV for over-sells or missing buy transactions
- Add correcting transaction if confirmed closed

**Cash balance mismatch?**
- Verify the latest-dated Running Balance in the CSV matches your account statement (row order no longer matters — the balance is taken from the newest date, not the last file row, #158)
- Check for duplicate entries in cash_flows table (reference must be unique)
- Ensure no data corruption in CSV (look for hidden characters)