# Extension Guide: How to Integrate a New Broker

Adding a new broker (Interactive Brokers, TD Ameritrade, Robinhood, etc.) allows Trader Agent to execute trades via different platforms.

## Architecture Overview

Trader Agent uses a broker adapter pattern:
- **Core Logic**: Position sizing, risk management, P&L tracking (independent of broker)
- **Broker Adapter**: Implements standard interface (place_order, get_positions, get_cash)
- **Broker Config**: Runtime selection (Alpaca, Interactive Brokers, etc.)

Each broker has an adapter module with consistent interface; core logic calls adapter methods. Switching brokers is a config change.

## Steps to Integrate a New Broker

### 1. Create a Broker Adapter Module

Create: `agents/trader/<broker>_adapter.py`

```python
from abc import ABC, abstractmethod
from models import Position, Order

class BrokerAdapter(ABC):
    """Standard interface for all brokers."""
    
    @abstractmethod
    def place_order(
        self,
        side: str,  # "buy" or "sell"
        ticker: str,
        quantity: int,
        price: float | None = None,  # None = market order, float = limit price
        order_type: str = "market",  # "market" or "limit"
    ) -> str:  # Returns order_id
        """Place an order. Returns order_id."""
        pass
    
    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get all open positions."""
        pass
    
    @abstractmethod
    def get_cash(self) -> float:
        """Get available cash in account."""
        pass
    
    @abstractmethod
    def get_order_status(self, order_id: str) -> dict:
        """Get status of order (filled, pending, rejected, etc.)."""
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel pending order. Returns True if successful."""
        pass


class <BrokerName>Adapter(BrokerAdapter):
    """Adapter for <Broker> API."""
    
    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.logger = logging.getLogger(__name__)
    
    def place_order(
        self,
        side: str,
        ticker: str,
        quantity: int,
        price: float | None = None,
        order_type: str = "market",
    ) -> str:
        """Place order via <Broker> API."""
        try:
            payload = {
                "symbol": ticker,
                "qty": quantity,
                "side": side,
                "type": order_type,
                "time_in_force": "day",
            }
            if price is not None:
                payload["limit_price"] = price
            
            response = requests.post(
                f"{self.base_url}/v1/orders",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            order_data = response.json()
            
            order_id = order_data["id"]
            self.logger.info(f"Order placed: {order_id} {side} {quantity} {ticker}")
            return order_id
        
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            raise
    
    def get_positions(self) -> list[Position]:
        """Fetch open positions from <Broker>."""
        try:
            response = requests.get(
                f"{self.base_url}/v1/positions",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            positions_data = response.json()
            
            positions = []
            for pos in positions_data:
                position = Position(
                    ticker=pos["symbol"],
                    entry_price=float(pos["avg_fill_price"]),
                    shares=int(pos["qty"]),
                    status="open",
                    # ... other fields ...
                )
                positions.append(position)
            
            return positions
        
        except Exception as e:
            self.logger.error(f"Failed to get positions: {e}")
            return []
    
    def get_cash(self) -> float:
        """Get available cash."""
        try:
            response = requests.get(
                f"{self.base_url}/v1/account",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            account = response.json()
            return float(account["buying_power"])
        
        except Exception as e:
            self.logger.error(f"Failed to get cash: {e}")
            return 0.0
    
    # Implement other abstract methods...
```

**Example: Interactive Brokers Adapter**
```python
class IBAdapter(BrokerAdapter):
    def __init__(self, host: str, port: int, client_id: int):
        self.client = IBApi.EClient(self)
        self.client.connect(host, port, client_id)
    
    def place_order(self, side, ticker, quantity, price=None, order_type="market"):
        contract = IBApi.Contract()
        contract.symbol = ticker
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        
        order = IBApi.Order()
        order.action = "BUY" if side == "buy" else "SELL"
        order.totalQuantity = quantity
        order.orderType = order_type.upper()
        if price:
            order.lmtPrice = price
        
        order_id = self.next_order_id
        self.client.placeOrder(order_id, contract, order)
        self.next_order_id += 1
        return str(order_id)
```

### 2. Define Broker Configuration

In `models.py`, add config class:

```python
class <BrokerName>Config(BaseModel):
    """Configuration for <Broker>."""
    api_key: str
    api_secret: str
    base_url: str
    # ... other broker-specific params ...
```

### 3. Add Adapter Factory to Trader Agent

In `agents/trader/trader_agent.py`, create factory function:

```python
from <broker>_adapter import <BrokerName>Adapter

def _create_broker_adapter(broker_name: str) -> BrokerAdapter:
    """Factory to instantiate broker adapter based on config."""
    broker_name = os.getenv("BROKER", "alpaca").lower()
    
    if broker_name == "alpaca":
        return AlpacaAdapter(
            api_key=os.getenv("ALPACA_API_KEY"),
            api_secret=os.getenv("ALPACA_API_SECRET"),
            base_url=os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets"),
        )
    
    elif broker_name == "ib":
        return IBAdapter(
            host=os.getenv("IB_HOST", "localhost"),
            port=int(os.getenv("IB_PORT", "7497")),
            client_id=int(os.getenv("IB_CLIENT_ID", "1")),
        )
    
    elif broker_name == "<broker>":
        return <BrokerName>Adapter(
            api_key=os.getenv("<BROKER>_API_KEY"),
            # ... other config ...
        )
    
    else:
        raise ValueError(f"Unknown broker: {broker_name}")

# Instantiate at module level
_broker = _create_broker_adapter(os.getenv("BROKER", "alpaca"))
```

### 4. Update Trader Agent to Use Adapter

In `TraderAgent` class, replace direct Alpaca calls with adapter calls:

```python
class TraderAgent(Agent):
    def execute_order(self, ticker: str, side: str, quantity: int, price: float | None = None) -> str:
        """Place order via current broker adapter."""
        try:
            order_id = _broker.place_order(
                side=side,
                ticker=ticker,
                quantity=quantity,
                price=price,
                order_type="limit" if price else "market",
            )
            self.logger.info(f"Order executed: {order_id}")
            return order_id
        except Exception as e:
            self.logger.error(f"Order failed: {e}")
            raise
    
    def get_positions(self) -> list[Position]:
        """Fetch positions from current broker."""
        return _broker.get_positions()
    
    def get_available_cash(self) -> float:
        """Get cash from current broker."""
        return _broker.get_cash()
```

### 5. Add Environment Configuration

In `.env`:

```
BROKER=<broker>
<BROKER>_API_KEY=your_key
<BROKER>_API_SECRET=your_secret
<BROKER>_BASE_URL=https://api.<broker>.com
```

In `.env.example`:

```
BROKER=alpaca
# Use one of: alpaca, ib, <broker>
<BROKER>_API_KEY=
<BROKER>_API_SECRET=
<BROKER>_BASE_URL=
```

### 6. Test the Adapter

```python
# Test adapter directly
adapter = <BrokerName>Adapter(
    api_key="test_key",
    api_secret="test_secret",
    base_url="https://..."
)

# Test in dry-run mode (no real orders)
order_id = adapter.place_order("buy", "AAPL", 10)
assert order_id, "Should return order ID"

positions = adapter.get_positions()
assert isinstance(positions, list)

cash = adapter.get_cash()
assert cash > 0

# Test Trader Agent with new broker
trader = TraderAgent()
# (Would execute real orders, test with dry_run=true)
```

### 7. Validate Risk Management Still Works

New broker must honor Trader Agent's risk limits:
- Position sizing: max_position_size = portfolio × 0.05
- Max loss per trade: 2% of portfolio
- Max open positions: 10
- Cash reserve: never use 100% of portfolio

Verify broker adapter respects these limits before order execution.

### 8. Update Spec (if Breaking Changes)

If new broker introduces constraints (e.g., minimum order size, no shorting), document in `trader-agent/spec.md`:

```
### Broker-Specific Constraints
- Interactive Brokers: minimum order = 1 share, accounts require TWS login
- TD Ameritrade: closes at 4:30pm ET (vs 4:00pm for others)
```

### 9. Document Broker Setup for Users

Create `docs/BROKERS.md`:

```markdown
## Supported Brokers

### Alpaca (Default)
1. Create account: https://alpaca.markets
2. Get API keys from dashboard
3. Set in .env:
   ```
   BROKER=alpaca
   ALPACA_API_KEY=...
   ALPACA_API_SECRET=...
   ```

### Interactive Brokers
1. Download TWS and set up account
2. Enable API in TWS settings (port 7497)
3. Set in .env:
   ```
   BROKER=ib
   IB_HOST=localhost
   IB_PORT=7497
   IB_CLIENT_ID=1
   ```

### Adding a New Broker
Follow agents/trader/README.md "Adding a Broker Adapter"
```

## Checklist

- [ ] Created `<broker>_adapter.py` implementing BrokerAdapter interface
- [ ] Implemented: place_order(), get_positions(), get_cash(), get_order_status(), cancel_order()
- [ ] All methods handle errors gracefully (try-except, return sensible defaults)
- [ ] Added <BrokerConfig> model to models.py
- [ ] Created adapter factory function in trader_agent.py
- [ ] Updated Trader Agent to use _broker adapter instead of hardcoded Alpaca
- [ ] Added environment configuration to .env/.env.example
- [ ] Tested adapter in isolation
- [ ] Tested Trader Agent with new broker
- [ ] Verified risk limits still enforced
- [ ] Created user documentation (BROKERS.md)
- [ ] Updated spec if broker introduces constraints

## Common Pitfalls

**Pitfall:** Assuming all brokers have same order types/fields
- Reason: Brokers differ (some no shorting, some no crypto, etc.)
- Fix: Document broker constraints in spec, validate at order time

**Pitfall:** Blocking Trader if broker API is slow
- Reason: Broker API calls may hang; blocks entire pipeline
- Fix: Add timeouts to all API calls, fail fast

**Pitfall:** Not implementing all abstract methods
- Reason: Trader Agent expects certain methods and crashes
- Fix: Implement full BrokerAdapter interface, raise NotImplementedError for unsupported features

**Pitfall:** Changing order response format per broker
- Reason: Trader expects consistent order_id format
- Fix: Normalize order_id to string, consistent across brokers

**Pitfall:** Not handling market hours restrictions
- Reason: Some brokers reject after-hours orders, some allow
- Fix: Document broker-specific hours in spec

**Pitfall:** Forgetting to update position P&L with broker actual prices
- Reason: Trader calculates P&L using entry price; should use actual fills
- Fix: After order fills, update Position.entry_price to actual fill price from broker
