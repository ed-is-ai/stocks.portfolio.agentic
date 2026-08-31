"""Force-refresh flag plumbing: route -> pipeline service -> orchestrator argv (#345)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import app
from app.core.security import require_local_or_token
from app.integrations import stocktwits_email
from app.integrations.stocktwits_email import StockTwitsEmailSource
from app.services.pipeline_service import PipelineRunResult, PipelineService
import app.services.pipeline_service as pipeline_service_module

client = TestClient(app)


# --------------------------------------------------------------------------- #
# /refresh-data route -> run_once                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def capture_run_once(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[Any, ...]] = []

    def _fake_run_once(self: PipelineService, *args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return PipelineRunResult(success=True, details="ok")

    monkeypatch.setattr(PipelineService, "run_once", _fake_run_once)
    monkeypatch.setattr(PipelineService, "missing_configuration", staticmethod(list))
    app.dependency_overrides[require_local_or_token] = lambda: None
    yield calls
    app.dependency_overrides.pop(require_local_or_token, None)


def test_refresh_data_defaults_both_flags_off(
    capture_run_once: list[tuple[Any, ...]],
) -> None:
    response = client.post("/refresh-data", data={})
    assert response.status_code == 200
    args, _kwargs = capture_run_once[0]
    # run_once(extract, force_whale_wisdom, force_stocktwits)
    assert args == (False, False, False)


def test_refresh_data_institutional_sets_extract_only(
    capture_run_once: list[tuple[Any, ...]],
) -> None:
    response = client.post("/refresh-data", data={"extract": "true"})
    assert response.status_code == 200
    args, _kwargs = capture_run_once[0]
    assert args == (True, False, False)


def test_refresh_data_forwards_set_flags(
    capture_run_once: list[tuple[Any, ...]],
) -> None:
    client.post(
        "/refresh-data",
        data={
            "extract": "true",
            "force_whale_wisdom": "true",
            "force_stocktwits": "true",
        },
    )
    args, _kwargs = capture_run_once[0]
    assert args == (True, True, True)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("force_whale_wisdom", (True, True, False)),
        ("force_stocktwits", (True, False, True)),
    ],
)
def test_refresh_data_custom_flags_are_independent(
    capture_run_once: list[tuple[Any, ...]], field: str, expected: tuple[bool, ...]
) -> None:
    client.post("/refresh-data", data={"extract": "true", field: "true"})
    args, _kwargs = capture_run_once[0]
    assert args == expected


def test_confirmation_retains_selected_custom_flags(
    monkeypatch: pytest.MonkeyPatch,
    capture_run_once: list[tuple[Any, ...]],
) -> None:
    monkeypatch.setattr(
        PipelineService,
        "missing_configuration",
        staticmethod(
            lambda: [SimpleNamespace(name="Provider", impact="Reduced coverage")]
        ),
    )

    response = client.post(
        "/refresh-data",
        data={
            "extract": "true",
            "force_whale_wisdom": "true",
            "force_stocktwits": "false",
        },
    )

    assert response.status_code == 200
    assert '"extract": true' in response.text
    assert '"force_whale_wisdom": true' in response.text
    assert '"force_stocktwits": false' in response.text
    assert capture_run_once == []


# --------------------------------------------------------------------------- #
# run_once -> orchestrator argv                                                #
# --------------------------------------------------------------------------- #


class _FakeCompleted:
    returncode = 0
    stdout = "done"
    stderr = ""


def _run_once_capturing_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **flags: bool
) -> list[str]:
    from app.repositories.pipeline_status_repo import PipelineStatusRepository

    repo = PipelineStatusRepository(tmp_path / "status.json")
    monkeypatch.setattr(pipeline_service_module, "_status_repository", repo)

    captured: dict[str, list[str]] = {}

    def _fake_subprocess_run(argv: list[str], **_kwargs: Any) -> _FakeCompleted:
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(pipeline_service_module.subprocess, "run", _fake_subprocess_run)
    PipelineService().run_once(extract=True, **flags)
    return captured["argv"]


def test_run_once_appends_force_whale_wisdom_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _run_once_capturing_argv(monkeypatch, tmp_path, force_whale_wisdom=True)
    assert "--extract" in argv
    assert "--force-whale-wisdom" in argv
    assert "--force-stocktwits" not in argv


def test_run_once_appends_force_stocktwits_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    argv = _run_once_capturing_argv(monkeypatch, tmp_path, force_stocktwits=True)
    assert "--force-stocktwits" in argv
    assert "--force-whale-wisdom" not in argv


# --------------------------------------------------------------------------- #
# StockTwitsEmailSource(force=True) ignores the watermark                       #
# --------------------------------------------------------------------------- #


def test_force_stocktwits_reprocesses_old_email(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    when = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    sample = (
        Path(__file__).resolve().parent / "fixtures" / "stocktwits_weekly_sample.html"
    ).read_text(encoding="utf-8")

    src = StockTwitsEmailSource(
        config=stocktwits_email.ImapConfig(
            host="h", port=993, user="u", password="p", sender="s", subject="x"
        ),
        api_key="key",
        watermark_path=tmp_path / "wm.json",
        force=True,
    )
    src._write_watermark(when)  # watermark == the email's received time

    monkeypatch.setattr(src, "_fetch_latest_email", lambda: (when, sample))
    monkeypatch.setattr(
        src,
        "_download_image",
        lambda url: stocktwits_email._Image(b"\xff\xd8\xff", "image/jpeg"),
    )
    monkeypatch.setattr(
        src, "_extract_tickers", lambda image, index_key: [f"{index_key.upper()}1"]
    )

    result = src.load()
    assert result is not None and result  # re-processed despite not being newer

    # A non-forced source with the same state skips.
    src.force = False
    assert src.load() is None
