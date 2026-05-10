"""
Unit tests for data models.
"""

import pytest
from models import StockScan, StockAnalysis, StockRecord, EmailConfig


class TestModels:
    """Test data models."""

    def test_stock_scan_creation(self):
        """Test StockScan model creation and validation."""
        scan = StockScan(
            ticker="AAPL",
            as_of="2024-01-01",
            price=150.0,
            volume=1000000,
            rel_volume=1.2,
            high_52w=200.0,
            low_52w=100.0,
            pct_from_52w_high=-25.0,
            pct_change_week=5.0
        )

        assert scan.ticker == "AAPL"
        assert scan.price == 150.0
        assert scan.volume == 1000000

    def test_stock_analysis_creation(self):
        """Test StockAnalysis model creation."""
        analysis = StockAnalysis(
            score=8,
            stage="Stage 2",
            entry_zone="approaching",
            strengths=["Strong momentum", "Good volume"],
            risks=["High valuation"],
            summary="Excellent setup"
        )

        assert analysis.score == 8
        assert analysis.stage == "Stage 2"
        assert analysis.entry_zone == "approaching"
        assert len(analysis.strengths) == 2
        assert len(analysis.risks) == 1

    def test_stock_record_creation(self):
        """Test StockRecord model with analysis."""
        scan = StockScan(
            ticker="TSLA",
            as_of="2024-01-01",
            price=200.0,
            volume=5000000,
            rel_volume=1.5,
            high_52w=300.0,
            low_52w=150.0,
            pct_from_52w_high=-33.3,
            pct_change_week=-2.0
        )

        analysis = StockAnalysis(
            score=6,
            stage="Stage 1",
            entry_zone="far",
            strengths=["Above SMA200"],
            risks=["Weak RSI", "Low volume"],
            summary="Mixed signals"
        )

        record = StockRecord.model_validate({
            **scan.model_dump(),
            "analysis": analysis.model_dump()
        })

        assert record.ticker == "TSLA"
        assert record.price == 200.0
        assert record.analysis.score == 6
        assert record.analysis.stage == "Stage 1"

    def test_email_config_creation(self):
        """Test EmailConfig model."""
        config = EmailConfig(
            host="smtp.gmail.com",
            port=587,
            user="test@example.com",
            password="password123",
            recipient="alerts@example.com"
        )

        assert config.host == "smtp.gmail.com"
        assert config.port == 587
        assert config.user == "test@example.com"

    def test_score_validation(self):
        """Test score range validation."""
        # Valid score
        analysis = StockAnalysis(score=7, stage="Stage 2", entry_zone="far",
                               strengths=[], risks=[], summary="Test")
        assert analysis.score == 7

        # Invalid score - too high
        with pytest.raises(ValueError):
            StockAnalysis(score=15, stage="Stage 2", entry_zone="far",
                         strengths=[], risks=[], summary="Test")

        # Invalid score - too low
        with pytest.raises(ValueError):
            StockAnalysis(score=0, stage="Stage 2", entry_zone="far",
                         strengths=[], risks=[], summary="Test")

    def test_model_serialization(self):
        """Test model JSON serialization."""
        scan = StockScan(
            ticker="NVDA",
            as_of="2024-01-01",
            price=500.0,
            volume=10000000,
            rel_volume=2.1,
            high_52w=600.0,
            low_52w=300.0,
            pct_from_52w_high=-16.7,
            pct_change_week=10.0
        )

        # Test serialization
        data = scan.model_dump()
        assert data['ticker'] == "NVDA"
        assert data['price'] == 500.0

        # Test deserialization
        scan2 = StockScan.model_validate(data)
        assert scan2.ticker == scan.ticker
        assert scan2.price == scan.price