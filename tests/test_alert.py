"""
Unit tests for AlertAgent.
"""

import pytest
import sqlite3
import os
from unittest.mock import patch, MagicMock

from agents.alert.alert_agent import AlertAgent
from models import EmailConfig, StockRecord, StockScan, StockAnalysis


class TestAlertAgent:
    """Test AlertAgent functionality."""

    @pytest.fixture
    def sample_high_score_stocks(self):
        """Create sample stocks with high scores."""
        stocks = []
        for i in range(3):
            scan = StockScan(
                ticker=f"HIGH{i}",
                as_of="2024-01-01",
                price=100.0 + i * 10,
                volume=1000000,
                rel_volume=1.2,
                high_52w=150.0,
                low_52w=50.0,
                pct_from_52w_high=-10.0,
                pct_change_week=5.0
            )

            analysis = StockAnalysis(
                score=9 - i,  # Scores: 9, 8, 7
                stage="Stage 2",
                near_entry=True,
                strengths=["Strong momentum"],
                risks=[],
                summary="High score stock"
            )

            record = StockRecord.model_validate({
                **scan.model_dump(),
                "analysis": analysis.model_dump()
            })
            stocks.append(record)

        return stocks

    @pytest.fixture
    def sample_low_score_stocks(self):
        """Create sample stocks with low scores."""
        stocks = []
        for i in range(2):
            scan = StockScan(
                ticker=f"LOW{i}",
                as_of="2024-01-01",
                price=50.0 + i * 5,
                volume=500000,
                rel_volume=0.8,
                high_52w=80.0,
                low_52w=30.0,
                pct_from_52w_high=-30.0,
                pct_change_week=-2.0
            )

            analysis = StockAnalysis(
                score=3 + i,  # Scores: 3, 4
                stage="Stage 4",
                near_entry=False,
                strengths=[],
                risks=["Weak momentum"],
                summary="Low score stock"
            )

            record = StockRecord.model_validate({
                **scan.model_dump(),
                "analysis": analysis.model_dump()
            })
            stocks.append(record)

        return stocks

    def test_should_alert_high_score(self, sample_high_score_stocks):
        """Test that high-score stocks should trigger alerts."""
        agent = AlertAgent()
        mock_conn = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        for stock in sample_high_score_stocks:
            assert agent.should_alert(stock, mock_conn) is True

    def test_should_alert_low_score(self, sample_low_score_stocks):
        """Test that low-score stocks should not trigger alerts."""
        agent = AlertAgent()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        for stock in sample_low_score_stocks:
            assert agent.should_alert(stock, mock_conn) is False

    def test_should_alert_boundary_cases(self):
        """Test boundary cases for alert threshold."""
        agent = AlertAgent()

        # Score exactly 8 - should alert
        analysis = StockAnalysis(
            score=8, stage="Stage 2", near_entry=True,
            strengths=[], risks=[], summary="Test"
        )
        scan = StockScan(
            ticker="TEST", as_of="2024-01-01", price=100.0, volume=1000000,
            rel_volume=1.0, high_52w=120.0, low_52w=80.0, pct_from_52w_high=-10.0, pct_change_week=0.0
        )
        stock = StockRecord.model_validate({
            **scan.model_dump(), "analysis": analysis.model_dump()
        })
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        assert agent.should_alert(stock, mock_conn) is True

        # Score 6 - below threshold, should not alert
        analysis.score = 6
        stock = StockRecord.model_validate({
            **scan.model_dump(), "analysis": analysis.model_dump()
        })
        assert agent.should_alert(stock, mock_conn) is False

    @patch('smtplib.SMTP')
    def test_send_email_success(self, mock_smtp, sample_high_score_stocks):
        """Test successful email sending."""
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        email_cfg = EmailConfig(
            host='localhost', port=1025, user='user@example.com',
            password='pass', recipient='recipient@example.com')
        agent = AlertAgent(email_config=email_cfg)
        subject = 'Test Alert'
        html_body = '<p>Test</p>'
        text_body = 'Test'

        agent.send_email(subject, html_body, text_body)

        # Verify SMTP was called correctly
        mock_smtp.assert_called_once_with('localhost', 1025)
        mock_server.sendmail.assert_called_once()

    @patch('smtplib.SMTP')
    def test_send_email_failure(self, mock_smtp, sample_high_score_stocks):
        """Test email sending failure handling."""
        mock_smtp.side_effect = Exception("SMTP Error")

        email_cfg = EmailConfig(
            host='localhost', port=1025, user='user@example.com',
            password='pass', recipient='recipient@example.com')
        agent = AlertAgent(email_config=email_cfg)
        subject = 'Test Alert'
        html_body = '<p>Test</p>'
        text_body = 'Test'

        # Should not raise exception
        agent.send_email(subject, html_body, text_body)

    def test_format_alert_html(self, sample_high_score_stocks):
        """Test HTML alert formatting."""
        agent = AlertAgent()
        html = agent.format_alert_html(sample_high_score_stocks[0])

        assert "HIGH0" in html
        assert "Score 9/10" in html
        assert "<html>" in html
        assert "</html>" in html
        assert "table" in html

    def test_run_method_no_alerts(self, sample_low_score_stocks):
        """Test run method with no alerts triggered."""
        agent = AlertAgent()
        agent.run(sample_low_score_stocks)

        # Should not attempt to send email
        # (We can't easily test this without mocking, but at least verify no exceptions)

    @patch('agents.alert.alert_agent.AlertAgent.send_email')
    def test_run_method_with_alerts(self, mock_send_email, tmp_path, sample_high_score_stocks):
        """Test run method with alerts triggered."""
        email_cfg = EmailConfig(
            host='localhost', port=1025, user='user@example.com',
            password='pass', recipient='recipient@example.com')
        agent = AlertAgent(email_config=email_cfg, db_path=str(tmp_path / "alerts.db"))
        agent.run(sample_high_score_stocks)

        assert mock_send_email.call_count == len(sample_high_score_stocks)
        assert mock_send_email.call_args[0][0].startswith('Momentum Alert: HIGH')

    def test_database_operations(self, sample_high_score_stocks):
        """Test database alert tracking."""
        # Clean up any existing test database
        if os.path.exists('test_alerts.db'):
            os.remove('test_alerts.db')

        agent = AlertAgent(name='AlertAgent', db_path='test_alerts.db')

        # First alert should be sent
        agent.run(sample_high_score_stocks[:1])
        # Second alert for same stock should be blocked by cooldown
        agent.run(sample_high_score_stocks[:1])

        # Verify database was created
        assert os.path.exists('test_alerts.db')

        # Clean up
        os.remove('test_alerts.db')

    def test_run_method_empty_list(self):
        """Test run method with empty stock list."""
        agent = AlertAgent()
        agent.run([])

        # Should not crash