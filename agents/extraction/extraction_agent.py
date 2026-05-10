"""
Extraction Agent — fetches the WisdomWise heat map JSON API for the top 50 most
actively held stocks across tracked 13F filers and returns tickers for the
scanner pipeline.

No authentication required. Uses the public heat map data endpoint.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import requests

from ms_agent_framework import Agent


HEAT_MAP_URL = "https://whalewisdom.com/api/heatmap_details.json"
# heat_map_id=3 → WhaleScore v2.0 (default, highest-quality filers)
# heat_map_id=1 → original heat map (broader universe)
HEAT_MAP_ID = 3
TOP_N = 50

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://whalewisdom.com/HeatMap",
}

HeatMapStock = dict[str, Any]

_EXTRACTION_RESULTS = Path(__file__).parent / "extraction_results.json"
_WW_CONTEXT = Path(__file__).parent / "ww_context.json"
_ST_WATCHLIST = (
    Path(__file__).parent.parent.parent / "config" / "stocktwits_watchlist.json"
)


class ExtractionAgent(Agent):
    """Aggregate watchlist from multiple sources: WhaleWisdom institutional data
    and quarterly-curated StockTwits momentum lists. Deduplicates and outputs
    source-tagged results for Scanner integration."""

    name: str = "ExtractionAgent"
    last_quarter: str = ""

    def run(self, payload: Any = None) -> list[str]:  # noqa: ARG002
        """Fetch WhaleWisdom and StockTwits tickers, merge, and update results.

        Returns combined list of tickers from both sources.
        """
        print("Fetching extraction sources...")

        # Fetch WhaleWisdom
        print(f"  WhaleWisdom: Fetching top {TOP_N} holdings...")
        ww_result = []
        try:
            stocks = self._fetch_heat_map()
            if stocks:
                ranked = sorted(
                    stocks, key=lambda s: int(s.get("overall_rank") or 9999)
                )
                tickers = [str(s["name"]) for s in ranked if s.get("name")]
                ww_result = list(dict.fromkeys(tickers))[:TOP_N]
                print(
                    f"    [ok] {len(ww_result)} tickers (quarter: {self.last_quarter})"
                )
                self._save_ww_context(ranked)
        except Exception as exc:
            print(f"    [err] {exc}")

        # Load StockTwits
        print("  StockTwits: Loading quarterly watchlist...")
        st_by_index = self._load_stocktwits_config()
        if st_by_index:
            st_total = sum(len(t) for t in st_by_index.values())
            print(f"    [ok] {st_total} tickers from config")
        else:
            print("    [warn] No StockTwits data loaded")

        # Update results with both sources
        self._update_results_with_sources(ww_result, st_by_index)

        # Return combined unique tickers
        all_tickers: set[str] = set(ww_result)
        for tickers in st_by_index.values():
            all_tickers.update(tickers)

        result = list(all_tickers)
        print(f"[ok] Extraction complete: {len(result)} total unique tickers")

        return result

    def _load_stocktwits_config(self) -> dict[str, list[str]]:
        """Load StockTwits watchlist config and return tickers by index.

        Returns dict: {"sp500": [...], "nasdaq100": [...], "russell2000": [...]}
        """
        if not _ST_WATCHLIST.exists():
            print(f"  [warn] StockTwits config not found at {_ST_WATCHLIST}")
            return {}

        try:
            data: dict[str, Any] = cast(
                dict[str, Any], json.loads(_ST_WATCHLIST.read_text(encoding="utf-8"))
            )
            indices = data.get("indices", {})
            result = {}
            for index_name, index_data in indices.items():
                tickers = index_data.get("tickers", [])
                if tickers:
                    result[index_name] = [str(t) for t in tickers]
            return result
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [err] Failed to load StockTwits config: {exc}")
            return {}

    def _fetch_heat_map(self) -> list[HeatMapStock]:
        """Call the heat map JSON endpoint and return the children list."""
        resp = requests.get(
            HEAT_MAP_URL,
            params={"heat_map_id": HEAT_MAP_ID},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        payload: dict[str, Any] = cast(dict[str, Any], resp.json())

        if not isinstance(payload, dict) or "children" in payload is False:
            keys = (
                list(payload.keys())
                if isinstance(payload, dict)
                else str(type(payload))
            )
            raise ValueError(f"Unexpected API response shape: {keys}")

        self.last_quarter = str(payload.get("name") or "")
        children = cast(list[HeatMapStock], payload["children"])
        print(f"  [net] {len(children)} stocks from heat map (id={HEAT_MAP_ID})")
        return children

    def _save_ww_context(self, ranked: list[HeatMapStock]) -> None:
        """Write per-ticker WhalWisdom buyer/seller counts to ww_context.json."""
        context = {
            str(s["name"]): {
                "filers_increasing": int(s.get("number_of_filers_increasing") or 0),
                "filers_decreasing": int(s.get("number_of_filers_decreasing") or 0),
                "ww_rank": int(s.get("overall_rank") or 0),
            }
            for s in ranked
            if s.get("name")
        }
        _WW_CONTEXT.write_text(json.dumps(context, indent=2), encoding="utf-8")

    def _update_results_with_sources(
        self,
        ww_tickers: list[str],
        st_by_index: dict[str, list[str]],
    ) -> None:
        """Update extraction_results.json with both WhaleWisdom and StockTwits.

        Replaces old StockTwits groups with new quarterly data.
        Keeps WhaleWisdom group for each run.
        """
        try:
            raw: list[str] | dict[str, list[str]] = json.loads(
                _EXTRACTION_RESULTS.read_text(encoding="utf-8")
            )
            data: dict[str, list[str]] = raw if isinstance(raw, dict) else {}
        except OSError:
            data = {}

        # Remove old StockTwits groups (clean slate for new quarterly data)
        updated_data = {k: v for k, v in data.items() if "stocktwits" not in k.lower()}

        # Add new StockTwits groups with today's date
        today = datetime.today().strftime("%Y-%m-%d")
        index_labels = {
            "sp500": "S&P 500",
            "nasdaq100": "NASDAQ 100",
            "russell2000": "Russell 2000",
        }
        for index_name, tickers in st_by_index.items():
            label = index_labels.get(index_name, index_name.upper())
            key = f"StockTwits Top 25 Momentum — {label} ({today})"
            updated_data[key] = tickers

        # Update WhaleWisdom group
        ww_label = self.last_quarter or "WhaleScore v2.0"
        ww_key = f"WisdomWise Heat Map — {ww_label} ({today})"
        updated_data[ww_key] = ww_tickers

        _EXTRACTION_RESULTS.write_text(
            json.dumps(updated_data, indent=2), encoding="utf-8"
        )

    def _update_results(self, new_tickers: list[str]) -> int:
        """Append tickers not already in any group to extraction_results.json.

        Creates a new dated WisdomWise source group for each batch of new tickers
        so the source and date are visible in the file. Returns the count added.
        """
        try:
            raw: list[str] | dict[str, list[str]] = json.loads(
                _EXTRACTION_RESULTS.read_text(encoding="utf-8")
            )
            data: dict[str, list[str]] = raw if isinstance(raw, dict) else {}
        except OSError:
            data = {}

        existing = {t for group in data.values() for t in group}
        to_add = [t for t in new_tickers if t not in existing]
        if not to_add:
            return 0

        today = datetime.today().strftime("%Y-%m-%d")
        label = self.last_quarter or "WhaleScore v2.0"
        key = f"WisdomWise Heat Map — {label} ({today})"
        if key in data:
            seen_in_key = set(data[key])
            data[key].extend(t for t in to_add if t not in seen_in_key)
        else:
            data[key] = to_add

        _EXTRACTION_RESULTS.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return len(to_add)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    agent = ExtractionAgent(name="ExtractionAgent")
    tickers = agent.run()

    print("\nTop holdings:")
    for i, ticker in enumerate(tickers, 1):
        print(f"  {i:>2}. {ticker}")
