"""Regenerate Excel from cached analysis_results.json without re-scanning."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.orchestration.orchestrator import EXCEL_OUTPUT, write_excel, load_source_map
from app.agents.scanner.scan_history import (
    get_fresh_breakouts,
    get_new_tickers,
    load_history,
)
from app.schemas import StockRecord

records = [
    StockRecord.model_validate(r)
    for r in json.loads(Path("agents/analyst/analysis_results.json").read_text())
]

source_map = load_source_map()
for r in records:
    st, ww = source_map.get(r.ticker, (False, False))
    r.in_stocktwits = st
    r.in_whale_wisdom = ww

history = load_history()
new = get_new_tickers([r.ticker for r in records], history)
breakouts = get_fresh_breakouts(records, history)
for r in records:
    if r.analysis and r.ticker in breakouts:
        r.analysis.fresh_breakout = True

write_excel(records, EXCEL_OUTPUT, new_tickers=new)
