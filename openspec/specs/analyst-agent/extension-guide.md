# Extension Guide: How to Add a New Scoring Framework

Adding a new scoring dimension to Analyst Agent allows evaluating stocks beyond CANSLIM (e.g., quality score, dividend metrics, growth profiling, ESG rating).

## Architecture Overview

Analyst Agent uses a modular scoring system:
- **CANSLIMScore**: Seven components (C, A, N, S, L, I, M) each 0-10
- **MomentumScore**: Stage, pattern details, entry proximity
- **Overall Score**: Weighted average of all dimensions

Each scoring framework is a separate Python module with a calculate() function that takes StockRecord → returns score dict.

## Steps to Add a New Scoring Framework

### 1. Create a Score Calculator Module

Create: `agents/analyst/<framework>_calculator.py`

```python
from models import StockRecord

class <FrameworkName>Calculator:
    """Calculate <framework> score components."""
    
    def calculate(self, stock: StockRecord) -> dict[str, float]:
        """
        Evaluate stock against <framework> criteria.
        
        Returns dict:
        {
            "score_name_1": float (0-10),
            "score_name_2": float (0-10),
            ...
        }
        
        All scores MUST be 0-10 range for weighting consistency.
        Return None or empty dict if data unavailable (graceful degradation).
        """
        try:
            score_1 = self._evaluate_metric_1(stock)
            score_2 = self._evaluate_metric_2(stock)
            return {
                "metric_1": score_1,
                "metric_2": score_2,
            }
        except Exception as e:
            self.logger.warning(f"Failed to calculate {framework} score: {e}")
            return {}
    
    def _evaluate_metric_1(self, stock: StockRecord) -> float:
        """Score metric on 0-10 scale."""
        # Implementation: evaluate based on StockRecord fields
        return float_value_0_to_10
    
    def _evaluate_metric_2(self, stock: StockRecord) -> float:
        """Score metric on 0-10 scale."""
        return float_value_0_to_10
```

**Example: Quality Score Calculator**
```python
class QualityCalculator:
    def calculate(self, stock: StockRecord) -> dict:
        # Evaluate ROE, profit margins, debt/equity, etc.
        roe_score = self._score_roe(stock.roe)
        debt_score = self._score_debt(stock.debt_equity_ratio)
        return {
            "roe_quality": roe_score,
            "debt_quality": debt_score,
        }
    
    def _score_roe(self, roe: float | None) -> float:
        if roe is None:
            return 0
        if roe > 0.20:  # Excellent
            return 10
        elif roe > 0.15:  # Good
            return 8
        elif roe > 0.10:  # Okay
            return 5
        else:
            return 3
```

### 2. Add Calculator Instantiation to Analyst

In `agents/analyst/analyst_agent.py`, instantiate at module level:

```python
from <framework>_calculator import <FrameworkName>Calculator

_<framework>_calculator = <FrameworkName>Calculator()
```

### 3. Integrate into Analyst.analyze() Loop

In the `analyze()` method, call calculator for each stock:

```python
def analyze(self, scans: list[StockRecord]) -> list[StockAnalysis]:
    results = []
    for stock in scans:
        analysis = StockAnalysis(ticker=stock.ticker, ...)
        
        # Existing scores
        analysis.canslim_score = _canslim_calc.calculate(stock)
        analysis.momentum_score = _vcp_calc.calculate(stock)
        
        # NEW FRAMEWORK
        <framework>_scores = _<framework>_calculator.calculate(stock)
        analysis.<framework>_score = <framework>_scores  # Store raw scores
        
        # Update overall score if framework is material
        analysis.overall_score = self._compute_overall_score(
            analysis.canslim_score,
            analysis.momentum_score,
            analysis.<framework>_score,  # Include new framework
        )
        
        results.append(analysis)
    return results
```

### 4. Add Score Fields to StockAnalysis Model

In `models.py`, add new fields to StockAnalysis:

```python
class <FrameworkName>Score(BaseModel):
    """<Framework> assessment components."""
    metric_1: float  # Description (0-10 scale)
    metric_2: float  # Description (0-10 scale)

class StockAnalysis(BaseModel):
    # ... existing fields ...
    
    # NEW FRAMEWORK
    <framework>_score: <FrameworkName>Score | None = None
```

### 5. Update Overall Score Weighting (if Material)

If new framework significantly impacts entry decisions, adjust weighting:

```python
def _compute_overall_score(
    self,
    canslim: CANSLIMScore,
    momentum: MomentumScore,
    <framework>: <FrameworkName>Score,
) -> int:
    """Compute weighted overall score (0-10)."""
    canslim_avg = sum(asdict(canslim).values()) / 7  # ~1.43 weight
    momentum_avg = ...
    <framework>_avg = sum(asdict(<framework>).values()) / n_metrics
    
    # Example weighting:
    overall = (
        canslim_avg * 0.5 +      # 50% CANSLIM
        momentum_avg * 0.3 +      # 30% Momentum
        <framework>_avg * 0.2     # 20% NEW FRAMEWORK
    )
    return int(round(overall))
```

If framework is supplementary (doesn't affect score), leave weighting unchanged. Store framework score separately for reference.

### 6. Update Entry Zone Logic (if Framework Affects Entry)

If new framework impacts entry timing, integrate into entry_zone determination:

```python
def _determine_entry_zone(self, stock: StockRecord, analysis: StockAnalysis) -> str:
    """Determine proximity to optimal entry."""
    # Existing logic
    base_zone = self._vcp_entry_zone(stock)
    
    # NEW FRAMEWORK MODIFIER (optional)
    if analysis.<framework>_score.some_metric < 3:
        # Framework signals caution, move zone to "getting_close"
        return "getting_close"
    
    return base_zone
```

### 7. Update Recommended Action Logic (if Framework Blocks Trades)

If framework can veto a buy (e.g., quality score too low), add filter:

```python
def _recommend_action(self, analysis: StockAnalysis) -> str:
    """Determine buy/hold/sell/watch."""
    # Base recommendation on CANSLIM + Momentum
    if analysis.score >= 8 and analysis.entry_zone == "approaching":
        base_action = "BUY"
    elif analysis.score >= 7:
        base_action = "WATCH"
    else:
        base_action = "HOLD"
    
    # NEW FRAMEWORK FILTER (optional)
    if analysis.<framework>_score.quality_metric < 2:
        # Quality too poor, downgrade to WATCH
        return "WATCH"
    
    return base_action
```

### 8. Update Spec with New Framework Details

In `analyst-agent/spec.md`, document:
- What the framework evaluates
- How it's calculated
- How it weights into overall score
- Extension point reference

Example addition to spec:
```
### Requirement: Analyst SHALL score stock quality
The system SHALL evaluate balance sheet quality (ROE, debt, margins)...
```

### 9. Test the Integration

```python
# Test calculator directly
calc = <FrameworkName>Calculator()
stock = StockRecord(...)
scores = calc.calculate(stock)
assert 0 <= scores["metric_1"] <= 10

# Test Analyst with new framework
analyst = AnalystAgent()
results = analyst.analyze([stock])
assert results[0].<framework>_score is not None
assert results[0].overall_score is not None
```

### 10. Validate Downstream Impact

If new framework affects recommended_action or entry_zone:
- Test that Alert Agent still filters correctly (checks exact string match)
- Test that Trader Agent receives valid recommendations
- Verify no breaking changes to dependent agents

## Checklist

- [ ] Created `<framework>_calculator.py` with calculate() method
- [ ] All scores in 0-10 range for consistency
- [ ] Handles missing data gracefully (returns empty dict)
- [ ] Added calculator instantiation to analyst_agent.py
- [ ] Added call to analyze() loop
- [ ] Created <FrameworkName>Score model in models.py
- [ ] Added score field to StockAnalysis
- [ ] Updated overall score weighting (if material)
- [ ] Tested calculator and Analyst integration
- [ ] Updated analyst-agent spec with new framework
- [ ] Tested downstream impact (Alert, Trader agents)

## Common Pitfalls

**Pitfall:** Returning scores outside 0-10 range
- Reason: Breaks weighting calculations, overall score becomes invalid
- Fix: Normalize all scores to 0-10 before returning

**Pitfall:** Not handling missing fields in StockRecord
- Reason: Framework crashes if expected field is None
- Fix: Check for None before using field, return 0 if missing

**Pitfall:** Changing overall score weighting without testing downstream
- Reason: May cause scores to spike, triggering unexpected alerts
- Fix: Test with real data, validate against historical scores

**Pitfall:** Not updating spec when framework affects recommendations
- Reason: Team doesn't understand why recommendations changed
- Fix: Document framework purpose and impact in spec

**Pitfall:** Making framework veto decisions (downgrading BUY to WATCH)
- Reason: Creates competing logic, hard to debug why trades fail
- Fix: Make framework supplementary (inform score, not veto); clear rules in spec
