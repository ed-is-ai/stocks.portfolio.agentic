# Market-Narrative Input Context

The user prompt is a plain-text rendering of deterministic, trustworthy figures — the
model is told to treat every figure as fact. It is assembled by `_build_user_prompt`
in `app/integrations/anthropic_client.py` from context objects the orchestrator computes
during a scan run (`app/orchestration/orchestrator.py`). Only the top `_TOP_N` (= 4)
rows of each ranked section are included.

## Sections, in order

Each line below shows the rendered format and where the underlying figure comes from.

### 1. As-of date
```
As of: <snapshot.as_of>
```

### 2. Sector prevalence this run
Source: `compute_sector_prevalence` → `SectorAllocationSnapshot.shares`
(`app/agents/scanner/sector_allocation.py`). "High-conviction" = score ≥ 7/10;
"MYB" = multi-year base breakout count.
```
Sector prevalence this run (share of candidates | high-conviction 7/10+ share of universe | multi-year breakouts):
- <sector>: <count_share%> | <strong_share%> 7/10+ | <myb_count> MYB
```

### 3. Week-on-week prevalence shift
Source: `with_week_deltas` → `SectorAllocationSnapshot.deltas` + `lookback_days`, computed
against a ~7-day-old baseline snapshot from the history store. First run has no baseline.
```
Week-on-week sector-prevalence shift (over ~<lookback_days> days):
- <sector>: <delta%> change
```
When there is no baseline yet:
```
No prior ~7-day baseline yet (first sector snapshot).
```
(When `lookback_days` is unknown the label is `Sector-prevalence shift vs. the prior run:`.)

### 4. Sectors with the most multi-year breakouts
Source: the same `shares`, filtered to `myb_count > 0`, ranked descending.
```
Sectors with the most multi-year breakouts this run:
- <sector>: <myb_count>
```

### 5. S&P 500 market breadth
Source: `fetch_market_breadth` → `MarketBreadth` (`app/integrations/market_breadth.py`).
Optional — omitted entirely when breadth is `None`. `trend` is rising/falling/flat-unknown;
`(stale)` appears when the reading is older than 7 days; the bearish-divergence clause only
appears when that flag is set.
```
S&P 500 market breadth: <pct_above_200dma>% of members above their 200DMA, trend <trend>[; bearish-divergence flag set] (as of <as_of>[ (stale)]).
```

### 6. Congressional / Senate net buying
Source: `top_congressional_buys` → `list[CongressionalBuy]`
(`app/agents/scanner/sector_allocation.py`). Net = buys − sells; only net-positive names,
ranked by congress then senate net. Omitted when empty.
```
Stocks most heavily net-bought by Congress/Senate (ticker | sector | congress net | senate net):
- <ticker> | <sector> | <congress_net signed> | <senate_net signed>
```

### 7. Portfolio sector weights
Source: `compute_portfolio_sector_weights` → `list[PortfolioSectorWeight]`, GBP-normalised
value share per sector. Omitted when empty.
```
Current portfolio sector weights:
- <sector>: <value_share%>
```

### 8. Market cycle (FOMC)
Source: `get_market_cycle_context` → `MarketCycleContext`
(`app/agents/scanner/market_cycle.py`). Static FOMC calendar; no network. The two day-offset
lines only appear when their values are known.
```
Market cycle phase: <phase>
Days since last FOMC decision: <days_since_last_decision>
Days to next FOMC meeting: <days_to_next_meeting>
```

### 9. Recent headlines
Source: `gather_news_context` → `NewsContext.items` (`app/agents/scanner/news_context.py`),
unioned from Alpha Vantage NEWS_SENTIMENT + GDELT, deduped by URL, recency-ranked, capped
at 15. Headline-level only (never article bodies). `sentiment` is `n/a` when absent. The
`domain` strings here are the only ones a bullet may legitimately cite.
```
Recent headlines (title | domain | url | sentiment):
- <title> | <domain> | <url> | <sentiment two-dp or n/a>
```

## Notes

- News is fetched **only when the LLM is enabled** (`ANTHROPIC_API_KEY` set), so GDELT
  latency and Alpha Vantage quota are not spent when the deterministic fallback will run
  anyway.
- The deterministic fallback (`build_deterministic_narrative`) consumes the same sector
  snapshot, portfolio weights, cycle, breadth, and congress inputs — everything except the
  news headlines — and never touches the network.
