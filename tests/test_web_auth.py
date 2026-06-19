import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.dependencies import get_trader_service
from web.app import app

client = TestClient(app)


def test_delete_trade_forbidden_for_non_loopback_without_token() -> None:
    # TestClient's client host is "testclient", not loopback, and no token set.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("APP_AUTH_TOKEN", None)
        resp = client.delete("/trades/1")
    assert resp.status_code == 403


def test_delete_trade_allowed_with_matching_token() -> None:
    mock_trader = MagicMock()
    app.dependency_overrides[get_trader_service] = lambda: mock_trader
    try:
        with patch.dict(os.environ, {"APP_AUTH_TOKEN": "s3cret"}):
            resp = client.delete(
                "/trades/1",
                headers={"X-Auth-Token": "s3cret"},
                follow_redirects=False,
            )
        assert resp.status_code != 403
        mock_trader.delete_trade.assert_called_once_with(1)
    finally:
        app.dependency_overrides.clear()


def test_refresh_data_forbidden_without_token() -> None:
    os.environ.pop("APP_AUTH_TOKEN", None)
    resp = client.post("/refresh-data")
    assert resp.status_code == 403
