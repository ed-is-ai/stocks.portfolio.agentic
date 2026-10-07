# Survivorship bias in S&P 500 backtests (#75)

Measured 2026-10-07 with `uv run python -m app.cli.survivorship_bias`.

## Question

How much does building history from **today's** S&P 500 list overstate a
backtest, compared with using the members the index **actually had** each
month?

## Method

- **Strategy.** All four portfolios use an equal-weight, monthly-rebalanced
  portfolio from 2000-01 to 2018-02. The window ends there because WIKI stops
  on 2018-03-27. The strategy is kept simple on purpose, so that only the
  universe differs between portfolios.
- **Portfolios:**
  - **today**: the 503 current members, each counted from when it has prices.
    This is what a backtest built from the current list sees.
  - **pit_yfinance**: each month's point-in-time members (#68), taken at the
    previous month's end, priced by yfinance only. Comparing it with *today*
    isolates the bias from **membership** alone.
  - **point_in_time**: the same members, adding delisted companies' prices
    from the WIKI archive (#70). Exits settle at their terminal price (#73)
    where one exists. Comparing it with *today* gives the bias from
    **membership plus delisted prices**.
  - **SPY**: for context.
- **Returns.** Returns are total returns, including dividends and splits.
  Reused tickers are left out of the point-in-time portfolios. Members
  without prices are counted in coverage, never priced by assumption.

## Result

| Portfolio | CAGR | Total | Max drawdown | 2000–02 drawdown | 2007–09 drawdown | Coverage |
|---|---|---|---|---|---|---|
| today | **+16.9%** | +1,604% | −47.9% | −23.1% | −47.9% | 83.6% |
| pit_yfinance | +12.4% | +741% | −49.5% | −25.8% | −49.5% | 55.2% |
| point_in_time | **+10.5%** | +517% | −53.5% | −27.0% | −53.5% | 85.1% |
| SPY | +5.4% | +159% | −50.8% | −44.7% | −50.8% | 100% |

- **Bias from membership alone: +4.5% a year.**
- **Bias with delisted prices as well: +6.4% a year.** Over 18 years that
  means 1,604% total return instead of 517%.

**Sanity check.** The point-in-time yearly returns track the published S&P
500 Equal Weight index closely, which suggests the data and method are sound:

| Year | Point-in-time | Published |
|---|---|---|
| 2003 | +40.6% | about +41% |
| 2008 | −37.5% | about −40% |
| 2009 | +46.0% | about +46% |

## Caveats

- **Still flattering.** About 15% of point-in-time member-months have no
  price in any source. They are mostly the failures (e.g. Lehman, Enron and
  WorldCom are not in WIKI), so the true bias is likely somewhat larger than
  6.4%.
- **Strategy-dependent.** The bias here is for a plain equal-weight index.
  Strategies that pick strong stocks (Minervini, Darvas) may be affected
  differently. Measuring them needs the backtest engine to use these rosters
  (#82).

## Conclusion

The bias is material: several percentage points a year, at the high end of
the published 1–4% range. That justifies wiring the point-in-time roster,
WIKI prices and terminal events into the backtest engine (#82), despite the
full historical rebuild it requires.
