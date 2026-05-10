# StockTwits Quarterly Watchlist Update Process

## Overview

The Extraction Agent integrates quarterly-curated StockTwits momentum stock lists alongside WhaleWisdom institutional data. This document describes how to maintain the StockTwits watchlist config.

## Schedule

**Update frequency:** Quarterly (every 3 months)  
**Next update due:** 2026-08-10

A scheduled reminder is set up at: https://claude.ai/code/routines/trig_01HJcnuq4oxKjDGJFBTv4Run

## Update Process

### 1. Access StockTwits Daily Rip
Visit https://thedailyrip.stocktwits.com/ and navigate to each index tab:
- S&P 500
- NASDAQ 100
- Russell 2000

### 2. Collect Top 25 Stocks
For each index, copy the top 25 momentum stocks from the "Top 25 Momentum" list. The page displays:
- Rank
- Ticker symbol
- Company name
- Sector
- Price
- Performance metrics

You only need the **ticker symbols**.

### 3. Update Configuration File

Edit `config/stocktwits_watchlist.json`:

```json
{
  "_comment": "Update quarterly from https://thedailyrip.stocktwits.com/. Next refresh due: 2026-11-10",
  "source": "StockTwits Daily Rip - Top 25 Momentum",
  "last_updated": "2026-08-10",
  "refresh_cadence": "quarterly",
  "description": "Curated top 25 momentum stocks from StockTwits for S&P 500, NASDAQ 100, and Russell 2000 indices. Updated quarterly by manually checking https://thedailyrip.stocktwits.com/",
  "indices": {
    "sp500": {
      "tickers": [
        "TICKER1", "TICKER2", ..., "TICKER25"
      ]
    },
    "nasdaq100": {
      "tickers": [
        "TICKER1", "TICKER2", ..., "TICKER25"
      ]
    },
    "russell2000": {
      "tickers": [
        "TICKER1", "TICKER2", ..., "TICKER25"
      ]
    }
  }
}
```

### 4. Update Metadata
- Set `last_updated` to today's date (YYYY-MM-DD format)
- Update `_comment` with the next refresh date (3 months ahead)

### 5. Commit and Push
```bash
git add config/stocktwits_watchlist.json
git commit -m "chore: update StockTwits quarterly watchlist (YYYY-MM-DD)"
git push
```

## How It's Used

Once the config is updated, the next extraction run will:
1. Load tickers from the updated config
2. Merge with WhaleWisdom data
3. Deduplicate across sources
4. Tag tickers with source flags (`in_stocktwits`, `in_whale_wisdom`)
5. Output source-grouped results to `extraction_results.json`

Scanner automatically detects source flags and enables filtering by source in analysis pipelines.

## Notes

- **No API integration:** This approach avoids Cloudflare blocking and API maintenance burden
- **Data quality:** StockTwits Daily Rip pre-curates the lists; all 75 tickers are valid
- **Quarterly cadence:** Aligns with typical portfolio rebalancing and provides momentum signals
- **Manual process:** Keeps the workflow simple and transparent

## Troubleshooting

**If you forget to update:**
- The extraction agent will continue using the previous quarter's data
- Set a calendar reminder or use the scheduled routine at https://claude.ai/code/routines/trig_01HJcnuq4oxKjDGJFBTv4Run

**If StockTwits layout changes:**
- Manually copy the top 25 from each index's momentum list
- Update the config file with new tickers
- No code changes needed
