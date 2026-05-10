# Extension Guide: How to Add a New Data Source

Adding a new data source to Scanner Agent enables enriching stocks with additional fundamental or technical data (earnings surprises, sentiment scores, supply chain risk, etc.).

## Architecture Overview

Scanner Agent integrates data from multiple sources:
- **yfinance**: Price, volume, basic fundamentals
- **Alpha Vantage**: Earnings data
- **Congress API**: Congressional/Senate trading activity
- **WhaleWisdom**: Institutional filer positions

Each source is a separate client module with a simple interface: fetch data → parse → return structured dict/object.

## Steps to Add a New Data Source

### 1. Create a Client Module

Create a new file: `agents/scanner/<source>_client.py`

```python
class <SourceName>Client:
    """Fetch <description> from <API/source>."""
    
    def __init__(self):
        self.api_key = os.getenv("<SOURCE_API_KEY>")
        self.base_url = "<API_URL>"
    
    def fetch_data(self, ticker: str) -> dict[str, float | int | None]:
        """
        Fetch data for a single ticker.
        
        Returns dict mapping field names to values:
        {
            "field_name_1": float_value,
            "field_name_2": int_value,
            ...
        }
        
        Return None or empty dict on failure (graceful degradation).
        """
        try:
            # Call API
            response = self._call_api(ticker)
            # Parse response
            return self._parse_response(response)
        except Exception as e:
            self.logger.warning(f"Failed to fetch {source} data for {ticker}: {e}")
            return {}
    
    def _call_api(self, ticker: str) -> Any:
        """Make API request."""
        # Implementation
        pass
    
    def _parse_response(self, response: Any) -> dict[str, float | int]:
        """Parse API response to dict."""
        # Implementation
        pass
```

**Example: Alpha Vantage Client**
```python
class AlphaVantageClient:
    def fetch_data(self, ticker: str) -> dict:
        data = requests.get(
            f"{self.base_url}/query",
            params={"symbol": ticker, "apikey": self.api_key}
        ).json()
        return {
            "earnings_surprise": data.get("earnings_surprise"),
            "surprise_pct": data.get("surprise_pct"),
        }
```

### 2. Add Client Initialization to Scanner

In `agents/scanner/scanner_agent.py`, instantiate the client at module level:

```python
from <source>_client import <SourceName>Client

_<source>_client = <SourceName>Client()
```

### 3. Integrate into Scanner.scan() Loop

In the `scan()` method, add a call for each ticker:

```python
def scan(self, watchlist: list[str]) -> list[StockRecord]:
    results = []
    for ticker in watchlist:
        stock = StockRecord(ticker=ticker, ...)
        
        # Existing sources
        stock.eps_growth = _av_client.fetch_data(ticker)["eps_growth"]
        stock.congress_buys = _congress_client.fetch_data(ticker)["buys"]
        
        # NEW SOURCE
        <source>_data = _<source>_client.fetch_data(ticker)
        stock.<new_field_1> = <source>_data.get("<field_1>")
        stock.<new_field_2> = <source>_data.get("<field_2>")
        
        results.append(stock)
    return results
```

### 4. Add Fields to StockRecord Model

In `models.py`, add new fields to StockRecord:

```python
class StockRecord(BaseModel):
    # ... existing fields ...
    
    # NEW SOURCE FIELDS
    <new_field_1>: float | None = None  # Description
    <new_field_2>: int | None = None    # Description
```

Type hints matter—use the correct type (float, int, bool, str) and mark as Optional (| None) since external APIs may fail.

### 5. Update Environment Configuration (if needed)

If the new source requires API credentials, add to `.env`:

```
<SOURCE_API_KEY>=your_key_here
```

And update `.env.example` with placeholder:
```
<SOURCE_API_KEY>=
```

### 6. Handle API Failures Gracefully

Always wrap API calls in try-except:
- Log warnings, not errors (expected during API outages)
- Return empty dict or None on failure
- Never crash the scan loop (other tickers still get scanned)

```python
def fetch_data(self, ticker: str) -> dict:
    try:
        # API call
        return parsed_result
    except requests.Timeout:
        self.logger.warning(f"Timeout fetching {source} for {ticker}")
        return {}
    except Exception as e:
        self.logger.warning(f"Error: {e}")
        return {}
```

### 7. Test the Integration

```python
# Test client directly
client = <SourceName>Client()
result = client.fetch_data("AAPL")
assert "field_name" in result

# Test Scanner with new source
scanner = ScannerAgent()
results = scanner.scan(["AAPL", "MSFT"])
assert results[0].<new_field_1> is not None or None  # Either populated or gracefully None
```

### 8. Downstream Usage (Optional)

If new fields should influence analysis, update Analyst Agent:
- Add field to CANSLIMScore or MomentumScore
- Adjust overall score weighting if significant
- Document decision in analyst-agent spec

## Checklist

- [ ] Created `<source>_client.py` with fetch_data() method
- [ ] Handles API failures gracefully (try-except, returns empty dict)
- [ ] Added client instantiation to scanner_agent.py
- [ ] Added fetch call to scan() loop
- [ ] Added new fields to StockRecord model with correct types
- [ ] Added environment variables to .env/.env.example
- [ ] Tested client and Scanner integration
- [ ] Updated specs if Analyst should use new fields
- [ ] Logged API calls for debugging

## Common Pitfalls

**Pitfall:** Returning 0 instead of None for missing data
- Reason: 0 is a valid value; None signals "unknown"
- Fix: Always use None for missing API data

**Pitfall:** Blocking Scanner if API is slow
- Reason: One slow API blocks all tickers
- Fix: Add timeout to API calls, return empty dict on timeout

**Pitfall:** Not handling API rate limits
- Reason: API will 429 and subsequent calls fail
- Fix: Add backoff/retry logic or cache results

**Pitfall:** Changing StockRecord schema without updating Analyst
- Reason: Analyst may fail to parse or expect certain fields
- Fix: Update both Scanner and Analyst specs together
