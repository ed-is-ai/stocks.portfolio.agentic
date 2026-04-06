"""
Unit tests for AnalystAgent.
"""

import pytest
from unittest.mock import patch, MagicMock

from analyst_agent import FOUNDRY_BASE_URL, AnalystAgent
from models import StockRecord, StockScan, StockAnalysis


class TestAnalystAgent:
    """Test AnalystAgent functionality."""

    def test_get_llm_client_unavailable(self):
        """Test LLM client when Foundry is not available."""
        agent = AnalystAgent()
        client = agent.get_llm_client()
        assert client is None

    @patch('analyst_agent.openai.OpenAI')
    def test_get_llm_client_available(self, mock_openai):
        """Test LLM client when Foundry is available."""
        mock_client = MagicMock()
        mock_client.models.list.return_value = []
        mock_openai.return_value = mock_client

        agent = AnalystAgent()
        client = agent.get_llm_client()

        assert client is not None
        mock_openai.assert_called_once_with(base_url=FOUNDRY_BASE_URL, api_key="foundry-local")

    def test_rule_based_score_high_score(self, sample_stock_record):
        """Test rule-based scoring for a high-scoring stock."""
        agent = AnalystAgent()

        # Create a stock with good characteristics
        scan = StockScan(
            ticker="TEST",
            as_of="2024-01-01",
            price=100.0,
            sma10=95.0,
            sma30=90.0,
            sma50=85.0,
            sma150=75.0,
            sma200=70.0,
            rsi14=70.0,
            atr14=2.0,
            volume=1000000,
            vol_ma50=900000,
            rel_volume=1.2,
            high_52w=110.0,
            low_52w=60.0,
            pct_from_52w_high=-5.0,  # Near high
            pct_change_week=3.0
        )

        record = StockRecord.model_validate(scan.model_dump())
        analysis = agent.rule_based_score(record)

        assert isinstance(analysis, StockAnalysis)
        assert 1 <= analysis.score <= 10
        assert analysis.stage in ["Stage 1", "Stage 2", "Stage 3", "Stage 4"]
        assert isinstance(analysis.near_entry, bool)

    def test_rule_based_score_low_score(self, sample_stock_record):
        """Test rule-based scoring for a low-scoring stock."""
        agent = AnalystAgent()

        # Create a stock with poor characteristics
        scan = StockScan(
            ticker="TEST",
            as_of="2024-01-01",
            price=50.0,  # Below SMAs
            sma10=60.0,
            sma30=65.0,
            sma50=70.0,
            sma150=80.0,
            sma200=85.0,
            rsi14=30.0,  # Low RSI
            atr14=2.0,
            volume=500000,
            vol_ma50=1000000,  # Low volume
            rel_volume=0.5,
            high_52w=100.0,
            low_52w=40.0,
            pct_from_52w_high=-50.0,  # Far from high
            pct_change_week=-5.0
        )

        record = StockRecord.model_validate(scan.model_dump())
        analysis = agent.rule_based_score(record)

        assert isinstance(analysis, StockAnalysis)
        assert 1 <= analysis.score <= 10
        assert analysis.stage in ["Stage 1", "Stage 2", "Stage 3", "Stage 4"]

    def test_score_stock_fallback_to_rule_based(self, sample_stock_record):
        """Test that score_stock falls back to rule-based when LLM unavailable."""
        agent = AnalystAgent()
        analysis = agent.score_stock(sample_stock_record, None)

        assert isinstance(analysis, StockAnalysis)
        assert 1 <= analysis.score <= 10

    @patch('openai.OpenAI')
    def test_score_stock_with_llm(self, mock_openai_class, sample_stock_record):
        """Test scoring with LLM when available."""
        # Mock the OpenAI client and response
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = '''
        {
            "score": 8,
            "stage": "Stage 2",
            "near_entry": true,
            "strengths": ["Strong momentum", "Good volume"],
            "risks": ["High valuation"],
            "summary": "Excellent setup"
        }
        '''
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai_class.return_value = mock_client

        agent = AnalystAgent()
        analysis = agent.score_stock(sample_stock_record, mock_client)

        assert analysis.score == 8
        assert analysis.stage == "Stage 2"
        assert analysis.near_entry == True
        assert len(analysis.strengths) == 2
        assert len(analysis.risks) == 1

    def test_run_method(self, sample_stock_record):
        """Test the run method with sample data."""
        agent = AnalystAgent()
        records = [sample_stock_record]

        results = agent.run(records)

        assert len(results) == 1
        assert results[0].analysis is not None
        assert isinstance(results[0].analysis.score, int)
        assert 1 <= results[0].analysis.score <= 10

    def test_run_method_empty_list(self):
        """Test run method with empty list."""
        agent = AnalystAgent()
        results = agent.run([])

        assert results == []

    def test_run_method_sorts_by_score(self):
        """Test that results are sorted by score descending."""
        agent = AnalystAgent()

        # Create records with different scores
        records = []
        for i in range(3):
            scan = StockScan(
                ticker=f"TEST{i}",
                as_of="2024-01-01",
                price=100.0 + i * 10,  # Different prices to get different scores
                volume=1000000,
                rel_volume=1.0,
                high_52w=150.0,
                low_52w=50.0,
                pct_from_52w_high=-20.0 + i * 5,
                pct_change_week=0.0
            )
            record = StockRecord.model_validate(scan.model_dump())
            records.append(record)

        results = agent.run(records)

        # Should be sorted by score descending
        scores = [r.analysis.score for r in results]
        assert scores == sorted(scores, reverse=True)