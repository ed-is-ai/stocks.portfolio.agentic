"""Shared fixtures for backtest tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.backtest.point_in_time_sources import write_sources


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Tmp membership, WIKI and override files for point-in-time rosters."""
    return write_sources(tmp_path)
