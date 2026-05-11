## 1. Backend — load cash history

- [x] 1.1 In `_load_portfolio_history()` (`web/app.py`), read the `cash_balance` column from each CSV row, defaulting to `None` if the column is absent
- [x] 1.2 Add `cash_values` list to the returned dict alongside `labels`, `values`, and `costs`

## 2. Backend — pass cash data to template

- [x] 2.1 In `_render_portfolio()`, extract `chart_data["cash_values"]` and pass it to the template context as `chart_cash` (JSON-serialised)

## 3. Template — add Cash dataset to chart

- [x] 3.1 In `_portfolio.html`, read `chart_cash` from context and assign to a JS variable `const cashVals`
- [x] 3.2 Add a Cash dataset to the Chart.js config: dashed green line (`#16a34a`), `borderDash: [4, 3]`, `fill: false`, `pointRadius: 0`, `label: 'Cash'`
- [x] 3.3 Update the tooltip callback to format the Cash line value as `£` with 2 decimal places (same as Market Value)
- [x] 3.4 Update the legend filter to show the Cash label (currently filters to show only Market Value and Cost Basis — add Cash to the allowed list)

## 4. Commit

- [ ] 4.1 Commit: `feat(portfolio): add cash line to portfolio value chart`
