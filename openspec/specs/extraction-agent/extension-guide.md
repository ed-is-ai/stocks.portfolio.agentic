# Extension Guide: How to Add a New Watchlist Source

Adding a new watchlist source (Finviz, FinanceAI, custom screener, etc.) extends the stock universe and adds selection perspective.

## Architecture Overview

Extraction Agent uses a pluggable source architecture:
- **Core Logic**: Union tickers, deduplicate, tag by source, quality gates (independent of sources)
- **Sources**: Fetchers for different screeners/APIs (plug-in implementations)
- **Configuration**: Runtime enable/disable sources, per-source quality gates

Each source is a module with get_tickers() and optional get_context() methods. Core logic aggregates sources, applies gates, outputs union watchlist.

## Steps to Add a New Watchlist Source

### 1. Create a Source Fetcher Module

Create: `agents/extraction/<source>_fetcher.py`

```python
class <SourceName>Fetcher:
    """Fetch stock tickers from <Source>."""
    
    def __init__(self, api_key: str = "", config: dict = None):
        self.api_key = api_key
        self.config = config or {}
        self.logger = logging.getLogger(__name__)
    
    def get_tickers(self) -> list[str]:
        """
        Fetch and return list of tickers from <Source>.
        
        Returns:
            list[str]: Unique tickers, unordered, deduplicated within this source.
        
        IMPORTANT: Never crash. Return empty list on failure (graceful degradation).
        """
        try:
            tickers = self._fetch_raw_data()
            tickers = self._apply_quality_gates(tickers)
            tickers = list(set(tickers))  # Deduplicate within source
            
            self.logger.info(f"Fetched {len(tickers)} tickers from {source}")
            return tickers
        
        except Exception as e:
            self.logger.warning(f"Failed to fetch from {source}: {e}")
            return []  # Graceful degradation
    
    def get_context(self) -> dict[str, dict]:
        """
        Optional: Return per-ticker context metadata.
        
        Returns:
            dict mapping ticker → dict with source-specific metadata:
            {
                "AAPL": {"rank": 1, "confidence": 0.95, "category": "tech"},
                "MSFT": {"rank": 2, "confidence": 0.92, "category": "tech"},
            }
        
        Can be empty dict if no context available.
        """
        return {}
    
    def _fetch_raw_data(self) -> list[str]:
        """Fetch tickers from source (raw, before quality gates)."""
        # Make API call, parse, return list of tickers
        pass
    
    def _apply_quality_gates(self, tickers: list[str]) -> list[str]:
        """Apply source-specific filters (min volume, price, market cap, etc.)."""
        # Filter list based on gates
        return filtered_tickers
```

**Example: Finviz Fetcher**
```python
class FinvizFetcher:
    def get_tickers(self) -> list[str]:
        """Fetch Finviz top gainers + most active."""
        try:
            url = "https://finviz.com/screener.ashx"
            params = {
                "v": 111,  # View: all stocks
                "f": "ta_pattern_bullish",  # Filter: bullish pattern
                "o": "volume",  # Sort: by volume
                "ar": "1_100",  # Activity range: 1-100M volume
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            # Parse HTML to extract ticker symbols
            soup = BeautifulSoup(response.content, "html.parser")
            rows = soup.find_all("tr", class_="styled-row")
            
            tickers = []
            for row in rows:
                cells = row.find_all("td")
                if cells:
                    ticker = cells[0].text.strip()
                    tickers.append(ticker)
            
            self.logger.info(f"Finviz: fetched {len(tickers)} tickers")
            return tickers
        
        except Exception as e:
            self.logger.warning(f"Finviz fetch failed: {e}")
            return []
    
    def get_context(self) -> dict:
        """Return Finviz pattern confidence."""
        # Could parse Finviz confidence scores if available
        return {}
```

### 2. Define Source Configuration

In `models.py`, add config class:

```python
class <SourceName>Config(BaseModel):
    """Configuration for <Source> fetcher."""
    enabled: bool = False
    api_key: str = ""
    # ... other params ...
    
    # Quality gates
    min_volume: int = 100_000  # Min avg volume
    min_price: float = 5.0     # Min stock price
    # ... other gates ...
```

### 3. Add Source Fetcher to Extraction Agent

In `agents/extraction/extraction_agent.py`, instantiate at module level:

```python
from <source>_fetcher import <SourceName>Fetcher

_sources = {
    "tv_screener": fetch_tv_screener_tickers,  # Existing TradingView
    "stocktwits": fetch_stocktwits_trending,   # Existing StockTwits
    "whale_wisdom": fetch_whale_wisdom,        # Existing WhaleWisdom
    "<source>": <SourceName>Fetcher(
        api_key=os.getenv("<SOURCE>_API_KEY", ""),
        config=<SourceName>Config(
            enabled=os.getenv("<SOURCE>_ENABLED", "false").lower() == "true",
            min_volume=int(os.getenv("<SOURCE>_MIN_VOLUME", "100000")),
        )
    ),
}
```

### 4. Integrate into Extraction Pipeline

In the `extract()` method, add source to union:

```python
def extract(self) -> dict[str, list[str]]:
    """Extract watchlist from all enabled sources."""
    all_tickers = set()
    source_map = {}
    context_data = {}
    
    for source_name, fetcher in _sources.items():
        if not fetcher.config.enabled:
            continue
        
        tickers = fetcher.get_tickers()
        context = fetcher.get_context()
        
        self.logger.info(f"{source_name}: {len(tickers)} tickers")
        
        # Union into all_tickers
        for ticker in tickers:
            all_tickers.add(ticker)
            if ticker not in source_map:
                source_map[ticker] = []
            source_map[ticker].append(source_name)
        
        # Merge context
        context_data.update(context)
    
    # Output union watchlist
    watchlist = sorted(list(all_tickers))
    self.logger.info(f"Total unique tickers: {len(watchlist)}")
    
    return {
        "watchlist": watchlist,
        "source_map": source_map,
        "context": context_data,
    }
```

### 5. Add Source Tagging to Output

Update extraction_results.json to track source membership:

```python
def save_results(self, watchlist: dict) -> None:
    """Save watchlist with source tags."""
    output = {
        "tickers": watchlist["watchlist"],
        "sources": watchlist["source_map"],  # Maps ticker → list of sources
        "timestamp": datetime.now().isoformat(),
    }
    
    with open("agents/extraction/extraction_results.json", "w") as f:
        json.dump(output, f, indent=2)
```

Updated extraction_results.json format:
```json
{
    "tickers": ["AAPL", "MSFT", "TSLA"],
    "sources": {
        "AAPL": ["tv_screener", "whale_wisdom"],
        "MSFT": ["tv_screener", "stocktwits"],
        "TSLA": ["stocktwits"]
    },
    "timestamp": "2025-02-15T10:30:00"
}
```

### 6. Update Scanner to Parse New Format

In `agents/scanner/scanner_agent.py`, update watchlist loader:

```python
def load_watchlist() -> list[str]:
    """Load tickers from extraction_results.json."""
    with open(EXTRACTION_RESULTS) as f:
        data = json.load(f)
    
    # Handle new format: dict with "tickers" key
    if isinstance(data, dict) and "tickers" in data:
        return data["tickers"]
    
    # Handle old format: simple list
    if isinstance(data, list):
        return data
    
    raise ValueError(f"Unknown extraction_results format: {type(data)}")

def load_source_map() -> dict[str, tuple[bool, bool]]:
    """Load per-ticker source tags."""
    with open(EXTRACTION_RESULTS) as f:
        data = json.load(f)
    
    if not isinstance(data, dict):
        return {}
    
    sources = data.get("sources", {})
    source_flags = {}
    
    for ticker, source_list in sources.items():
        source_flags[ticker] = (
            "stocktwits" in source_list,
            "whale_wisdom" in source_list,
        )
    
    return source_flags
```

### 7. Add Environment Configuration

In `.env`:

```
<SOURCE>_ENABLED=true
<SOURCE>_API_KEY=your_key
<SOURCE>_MIN_VOLUME=100000
```

In `.env.example`:

```
<SOURCE>_ENABLED=false
<SOURCE>_API_KEY=
<SOURCE>_MIN_VOLUME=100000
```

### 8. Test the Source Integration

```python
# Test fetcher directly
fetcher = <SourceName>Fetcher(
    api_key="test_key",
    config=<SourceName>Config(enabled=True)
)

tickers = fetcher.get_tickers()
assert isinstance(tickers, list)
assert all(isinstance(t, str) for t in tickers)
assert len(tickers) > 0

# Test Extraction Agent with new source
extractor = ExtractionAgent()
results = extractor.extract()
assert len(results["watchlist"]) > 0
assert all(isinstance(t, str) for t in results["watchlist"])

# Test Scanner loads new format
scanner = ScannerAgent()
watchlist = scanner.load_watchlist()
assert len(watchlist) > 0
```

### 9. Monitor Source Health

Add logging to track source reliability:

```python
def extract(self) -> dict:
    """Extract with source health tracking."""
    source_stats = {}
    
    for source_name, fetcher in _sources.items():
        if not fetcher.config.enabled:
            continue
        
        start = time.time()
        tickers = fetcher.get_tickers()
        elapsed = time.time() - start
        
        source_stats[source_name] = {
            "tickers": len(tickers),
            "elapsed_seconds": elapsed,
            "status": "success" if tickers else "failed",
        }
        
        self.logger.info(f"{source_name}: {source_stats[source_name]}")
    
    # Log aggregated stats
    total_tickers = len(all_tickers)
    self.logger.info(f"Extraction complete: {total_tickers} unique tickers")
    
    return results
```

### 10. Update Spec (Optional)

Document new source in `extraction-agent/spec.md`:

```
### Requirement: Extraction Agent SHALL source tickers from Finviz
Finviz stocks meeting bullish pattern criteria are included in union...
```

## Checklist

- [ ] Created `<source>_fetcher.py` with get_tickers() method
- [ ] Handles API failures gracefully (try-except, returns empty list)
- [ ] Added <SourceConfig> model to models.py
- [ ] Added fetcher instantiation to extraction_agent.py
- [ ] Integrated into extract() method
- [ ] Updated extraction_results.json format to include source tags
- [ ] Updated Scanner.load_watchlist() to parse new format
- [ ] Updated Scanner.load_source_map() to extract source tags
- [ ] Added environment configuration to .env/.env.example
- [ ] Tested fetcher in isolation
- [ ] Tested Extraction Agent with new source
- [ ] Tested Scanner compatibility with new format
- [ ] Added source health logging
- [ ] Updated spec if source is significant

## Common Pitfalls

**Pitfall:** Fetcher crashes instead of returning empty list
- Reason: One failed source kills entire extraction
- Fix: Always wrap in try-except, return [] on failure

**Pitfall:** Source has very high latency (blocks entire extraction)
- Reason: One slow API holds up pipeline
- Fix: Add timeout to all API calls (e.g., timeout=5), fail fast

**Pitfall:** Source returns duplicate tickers within itself
- Reason: Duplicates inflate watchlist size
- Fix: Deduplicate within source: `list(set(tickers))`

**Pitfall:** Scanner breaks when source tag changes (e.g., in_whale_wisdom field disappears)
- Reason: Analyst may reference source tags
- Fix: Keep source tag fields stable; add new fields, don't remove

**Pitfall:** Not validating ticker symbols
- Reason: Invalid symbols passed to yfinance crash Scanner
- Fix: Validate ticker format before returning (uppercase, 1-5 chars, alphanumeric)

**Pitfall:** Source has rate limits but no throttling
- Reason: Requests get 429'd, source returns nothing
- Fix: Add backoff/retry logic or cache results with TTL

**Pitfall:** Too many sources cause extraction to run > 1 minute
- Reason: Orchestrator expects extraction within time budget
- Fix: Parallelize source fetches using concurrent.futures or add selective enabled flags
