## Context

The SIPP import assigns synthetic tickers to some fund transactions because the CSV uses a description-matched pattern rather than a standard exchange symbol. Yahoo Finance has no listing for these synthetic tickers, so the price refresh returns `NaN`. Users need a way to map such internal tickers to their real Yahoo Finance equivalents.

Current price refresh flow:
1. Fetch all portfolio tickers from DB
2. `yf.download(tickers)` — fails silently for unknown symbols
3. Retry missing tickers with `.L` suffix — still fails for synthetic tickers

## Goals / Non-Goals

**Goals:**
- Allow users to declare `internal_ticker → yfinance_symbol` mappings in a config file
- Apply mappings transparently at refresh time — no code change required when adding a new alias
- File is gitignored so it holds personal portfolio-specific data without leaking it

**Non-Goals:**
- UI for editing the alias file (plain JSON is sufficient)
- Validating whether the mapped symbol actually exists on yfinance
- Storing aliases in the database

## Decisions

### 1. Config file location: `data/ticker_aliases.json`
`data/` already holds user-specific files (`processed/SIPP/`). Placing the alias file here keeps all user data in one directory and out of the codebase. File is created empty on first run if absent.

### 2. Format: flat JSON object `{ "INTERNAL": "YF_SYMBOL" }`
Simple key→value is the minimum viable format. No nesting, no comments. Easy to hand-edit.

Example (tickers replaced with placeholders — add your own):
```json
{
  "FUND_A": "0PXXXXXXXXX.L"
}
```

### 3. Applied before the `.L` retry logic
The existing `.L` retry already handles most UK stocks. Aliases are checked first so explicit overrides take priority. This means:
1. Remap any tickers that have an alias
2. Fetch original tickers (minus remapped ones) from yfinance
3. Retry remaining NaN tickers with `.L`

### 4. Missing file is a no-op, not an error
If `ticker_aliases.json` is absent, refresh works exactly as today. Avoids breaking a fresh install.

## Risks / Trade-offs

| Risk | Mitigation |
|------|-----------|
| Wrong alias entered by user | yfinance returns NaN; position shows `—`; same graceful behaviour as today |
| yfinance symbol is unknown to user | User looks up symbol on finance.yahoo.com |
| File gets committed with real portfolio data | `data/ticker_aliases.json` is in `.gitignore` |
