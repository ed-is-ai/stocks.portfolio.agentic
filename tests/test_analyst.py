"""
Unit tests for AnalystAgent.
"""

import pytest
from unittest.mock import patch, MagicMock

from agents.analyst.analyst_agent import FOUNDRY_BASE_URL, AnalystAgent
from models import CANSLIMScore, MomentumScore, StockAnalysis, StockRecord, StockScan


def _make_scan(**overrides) -> StockRecord:
    """Build a StockRecord with sensible defaults, applying any overrides."""
    defaults = dict(
        ticker="TEST",
        as_of="2024-01-01",
        price=100.0,
        sma10=95.0,
        sma30=90.0,
        sma50=85.0,
        sma150=75.0,
        sma200=70.0,
        rsi14=65.0,
        atr14=2.0,
        volume=1_000_000,
        vol_ma50=900_000,
        rel_volume=1.2,
        high_52w=110.0,
        low_52w=60.0,
        pct_from_52w_high=-9.1,
        pct_change_week=2.5,
    )
    defaults.update(overrides)
    return StockRecord.model_validate(StockScan(**defaults).model_dump())


class TestAnalystAgent:
    """Test AnalystAgent functionality."""

    @patch("agents.analyst.analyst_agent.openai.OpenAI")
    def test_get_llm_client_unavailable(self, mock_openai):
        """Test LLM client returns None when Foundry connection fails."""
        mock_openai.side_effect = Exception("connection refused")
        agent = AnalystAgent()
        assert agent.get_llm_client() is None

    @patch("agents.analyst.analyst_agent.openai.OpenAI")
    def test_get_llm_client_available(self, mock_openai):
        """Test LLM client when Foundry is available."""
        mock_model = MagicMock()
        mock_model.id = "phi-4-mini-test"
        mock_client = MagicMock()
        mock_client.models.list.return_value.data = [mock_model]
        mock_openai.return_value = mock_client

        agent = AnalystAgent()
        result = agent.get_llm_client()

        assert result is not None
        client, model_id = result
        assert model_id == "phi-4-mini-test"
        mock_openai.assert_called_once_with(base_url=FOUNDRY_BASE_URL, api_key="foundry-local")

    def test_rule_based_score_high_score(self):
        """Analysis has both canslim and momentum scores for a strong stock."""
        record = _make_scan(
            price=100.0,
            sma10=95.0, sma30=90.0, sma50=85.0, sma150=75.0, sma200=70.0,
            rsi14=70.0, rel_volume=1.2, pct_from_52w_high=-5.0, pct_change_week=3.0,
        )
        agent = AnalystAgent()
        analysis = agent.rule_based_score(record)

        assert isinstance(analysis, StockAnalysis)
        assert 1 <= analysis.score <= 10
        assert analysis.stage in ["Stage 1", "Stage 2", "Stage 3", "Stage 4"]
        assert isinstance(analysis.near_entry, bool)
        assert isinstance(analysis.canslim, CANSLIMScore)
        assert isinstance(analysis.momentum, MomentumScore)

    def test_rule_based_score_low_score(self):
        """Low-scoring stock still produces valid analysis with both scores."""
        record = _make_scan(
            price=50.0,
            sma10=60.0, sma30=65.0, sma50=70.0, sma150=80.0, sma200=85.0,
            rsi14=30.0, rel_volume=0.5, pct_from_52w_high=-50.0, pct_change_week=-5.0,
        )
        agent = AnalystAgent()
        analysis = agent.rule_based_score(record)

        assert isinstance(analysis, StockAnalysis)
        assert 1 <= analysis.score <= 10
        assert analysis.stage in ["Stage 1", "Stage 2", "Stage 3", "Stage 4"]
        assert isinstance(analysis.canslim, CANSLIMScore)
        assert isinstance(analysis.momentum, MomentumScore)

    def test_score_stock_fallback_to_rule_based(self, sample_stock_record):
        """score_stock falls back to rule-based when LLM unavailable."""
        agent = AnalystAgent()
        analysis = agent.score_stock(sample_stock_record, None)

        assert isinstance(analysis, StockAnalysis)
        assert 1 <= analysis.score <= 10

    @patch("openai.OpenAI")
    def test_score_stock_with_llm(self, mock_openai_class, sample_stock_record):
        """LLM path parses JSON response and returns StockAnalysis."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = """{
            "score": 8,
            "stage": "Stage 2",
            "near_entry": true,
            "strengths": ["Strong momentum", "Good volume"],
            "risks": ["High valuation"],
            "summary": "Excellent setup"
        }"""
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai_class.return_value = mock_client

        agent = AnalystAgent()
        analysis = agent.score_stock(sample_stock_record, (mock_client, "phi-4-mini-test"))

        assert analysis.score == 8
        assert analysis.stage == "Stage 2"
        assert analysis.near_entry is True
        assert len(analysis.strengths) == 2
        assert len(analysis.risks) == 1

    @patch.object(AnalystAgent, "get_llm_client", return_value=None)
    def test_run_method(self, _mock_llm, sample_stock_record):
        """run() produces analysis with both canslim and momentum scores."""
        agent = AnalystAgent()
        results = agent.run([sample_stock_record])

        assert len(results) == 1
        assert results[0].analysis is not None
        assert 1 <= results[0].analysis.score <= 10
        assert isinstance(results[0].analysis.canslim, CANSLIMScore)
        assert isinstance(results[0].analysis.momentum, MomentumScore)

    def test_run_method_empty_list(self):
        """run() with empty input returns empty list."""
        agent = AnalystAgent()
        assert agent.run([]) == []

    @patch.object(AnalystAgent, "get_llm_client", return_value=None)
    def test_run_method_sorts_by_canslim_then_momentum(self, _mock_llm):
        """Results are sorted by CANSLIM total desc, momentum total desc, score desc."""
        agent = AnalystAgent()
        records = [_make_scan(ticker=f"T{i}", price=100.0 + i * 10) for i in range(3)]
        results = agent.run(records)

        canslim_totals = [r.analysis.canslim.total for r in results if r.analysis and r.analysis.canslim]
        assert canslim_totals == sorted(canslim_totals, reverse=True)

    # --- True CANSLIM fundamental scoring ---

    def test_canslim_fundamental_score_strong(self):
        """High fundamentals produce a high CANSLIM score."""
        record = _make_scan(
            eps_growth=0.35,          # C: 2 (≥25%)
            roe=0.25,                 # A: 2 (≥17%)
            pct_from_52w_high=-3.0,  # N: 2 (≥-5%)
            rel_volume=1.8,           # S: 2 (≥1.5x)
            rel_strength_vs_spy=25.0, # L: 2 (≥20pp)
            inst_ownership_pct=0.60,  # I: 2 (≥50%)
            spy_uptrend=True,         # M: 2
        )
        agent = AnalystAgent()
        score = agent._canslim_fundamental_score(record)

        assert score.C == 2
        assert score.A == 2
        assert score.N == 2
        assert score.S == 2
        assert score.L == 2
        assert score.I == 2
        assert score.M == 2
        assert score.total == 14

    def test_canslim_fundamental_score_missing_data(self):
        """Missing fundamental data (None) gracefully scores 0 for that letter."""
        record = _make_scan(
            eps_growth=None,
            roe=None,
            rel_strength_vs_spy=None,
            inst_ownership_pct=None,
            spy_uptrend=None,
        )
        agent = AnalystAgent()
        score = agent._canslim_fundamental_score(record)

        assert score.C == 0  # no eps data
        assert score.A == 0  # no roe data
        assert score.L == 0  # no rel strength data
        assert score.I == 0  # no inst ownership data
        assert score.M == 0  # spy_uptrend is None → falsy

    def test_canslim_fundamental_score_moderate(self):
        """Partial fundamentals produce mid-range CANSLIM score."""
        record = _make_scan(
            eps_growth=0.12,          # C: 1 (≥10%)
            roe=0.13,                 # A: 1 (≥10%)
            pct_from_52w_high=-10.0, # N: 1 (≥-15%)
            rel_volume=1.1,           # S: 1 (≥1.0x)
            rel_strength_vs_spy=5.0,  # L: 1 (≥0pp)
            inst_ownership_pct=0.40,  # I: 1 (≥30%)
            spy_uptrend=True,         # M: 2
        )
        agent = AnalystAgent()
        score = agent._canslim_fundamental_score(record)

        assert score.C == 1
        assert score.A == 1
        assert score.N == 1
        assert score.S == 1
        assert score.L == 1
        assert score.I == 1
        assert score.M == 2
        assert score.total == 8

    def test_canslim_spy_downtrend_scores_zero_m(self):
        """SPY in downtrend (spy_uptrend=False) scores M=0."""
        record = _make_scan(spy_uptrend=False)
        agent = AnalystAgent()
        score = agent._canslim_fundamental_score(record)
        assert score.M == 0

    def test_momentum_score_above_sma150_scores_high_i(self):
        """Price above SMA150 (with no slope data) gives I=2 in momentum score."""
        record = _make_scan(
            price=100.0,
            sma10=95.0, sma30=90.0, sma50=85.0, sma150=75.0, sma200=70.0,
        )
        agent = AnalystAgent()
        analysis = agent.rule_based_score(record)

        assert analysis.momentum is not None
        assert analysis.momentum.I == 2  # price > SMA150, slope unknown → positive

    def test_strip_think_removes_reasoning_block(self):
        """_strip_think removes <think>...</think> blocks from LLM output."""
        raw = "<think>Let me reason...</think>\n{\"score\": 7}"
        result = AnalystAgent._strip_think(raw)
        assert result == '{"score": 7}'
