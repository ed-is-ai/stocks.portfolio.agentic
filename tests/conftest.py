"""
Test configuration and fixtures for stock agent tests.
"""

import pytest
import pandas as pd
from datetime import datetime, timedelta

from models import StockRecord, StockAnalysis, StockScan


@pytest.fixture
def sample_stock_data():
    """Sample stock data for testing."""
    return pd.DataFrame({
        'close': [100.0, 101.0, 102.0, 103.0, 104.0],
        'high': [105.0, 106.0, 107.0, 108.0, 109.0],
        'low': [95.0, 96.0, 97.0, 98.0, 99.0],
        'open': [99.0, 100.0, 101.0, 102.0, 103.0],
        'volume': [1000000, 1100000, 1200000, 1300000, 1400000]
    })


@pytest.fixture
def sample_stock_record():
    """Sample StockRecord for testing."""
    scan = StockScan(
        ticker="TEST",
        as_of="2024-01-01",
        price=100.0,
        sma10=98.0,
        sma30=95.0,
        sma50=92.0,
        sma150=85.0,
        sma200=80.0,
        rsi14=65.0,
        atr14=2.5,
        volume=1000000,
        vol_ma50=950000,
        rel_volume=1.05,
        high_52w=110.0,
        low_52w=70.0,
        pct_from_52w_high=-9.1,
        pct_change_week=2.5
    )

    analysis = StockAnalysis(
        score=7,
        stage="Stage 2",
        near_entry=True,
        strengths=["Strong momentum", "Above key SMAs"],
        risks=["High valuation"],
        summary="Good setup with strong technicals"
    )

    return StockRecord.model_validate({**scan.model_dump(), "analysis": analysis.model_dump()})


@pytest.fixture
def mock_yfinance(monkeypatch):
    """Mock yfinance download function."""
    def mock_download(*args, **kwargs):
        # Return sample data for any ticker
        dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
        return pd.DataFrame({
            'Close': [100 + i * 0.1 for i in range(100)],
            'High': [105 + i * 0.1 for i in range(100)],
            'Low': [95 + i * 0.1 for i in range(100)],
            'Open': [99 + i * 0.1 for i in range(100)],
            'Volume': [1000000] * 100
        }, index=dates)

    monkeypatch.setattr('yfinance.download', mock_download)