"""Unit tests for IntegrationConfigService (#47)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading

import pytest

from app.services.integration_config_service import (
    ALLOWED_KEYS,
    IntegrationConfigService,
    InvalidEnvPathError,
    UnknownConfigKeyError,
)


@pytest.fixture(autouse=True)
def _clean_environ():
    """Isolate from both other tests and this machine's real .env.

    ``app.services.pipeline_service`` loads the real ``.env`` at import
    time, so allowlisted keys may already be set — clear them before each
    test too, not just restore after, so "not configured" is meaningful.
    """
    saved = {key: os.environ.get(key) for key in ALLOWED_KEYS}
    for key in ALLOWED_KEYS:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_status_reports_missing_for_unset_keys(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    status = service.status()
    assert set(status) == set(ALLOWED_KEYS)
    assert all(not entry["configured"] for entry in status.values())


def test_update_writes_allowed_key_and_reports_configured(tmp_path) -> None:
    env_path = tmp_path / ".env"
    service = IntegrationConfigService(env_path)

    service.update({"FMP_API_KEY": "sk-123"})

    assert service.status()["FMP_API_KEY"]["configured"] is True
    assert "value" not in service.status()["FMP_API_KEY"]
    assert "sk-123" in env_path.read_text(encoding="utf-8")
    assert os.environ["FMP_API_KEY"] == "sk-123"


def test_displayable_keys_report_their_value(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    service.update({"EMAIL_HOST": "smtp.example.com", "EMAIL_PORT": "587"})
    status = service.status()
    assert status["EMAIL_HOST"]["value"] == "smtp.example.com"
    assert status["EMAIL_PORT"]["value"] == "587"


def test_update_rejects_unknown_key(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    with pytest.raises(UnknownConfigKeyError):
        service.update({"AWS_SECRET_ACCESS_KEY": "leaked"})


def test_clear_rejects_unknown_key(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    with pytest.raises(UnknownConfigKeyError):
        service.update({}, clear_keys=["SSH_PRIVATE_KEY"])


def test_blank_value_leaves_existing_key_unchanged(tmp_path) -> None:
    env_path = tmp_path / ".env"
    service = IntegrationConfigService(env_path)
    service.update({"FMP_API_KEY": "sk-123"})

    service.update({"FMP_API_KEY": "", "ALPHA_VANTAGE_API_KEY": "av-1"})

    assert service.status()["FMP_API_KEY"]["configured"] is True
    assert "sk-123" in env_path.read_text(encoding="utf-8")
    assert service.status()["ALPHA_VANTAGE_API_KEY"]["configured"] is True


def test_explicit_clear_removes_key_from_file_and_environ(tmp_path) -> None:
    env_path = tmp_path / ".env"
    service = IntegrationConfigService(env_path)
    service.update({"FMP_API_KEY": "sk-123"})

    service.update({}, clear_keys=["FMP_API_KEY"])

    assert service.status()["FMP_API_KEY"]["configured"] is False
    assert "FMP_API_KEY" not in env_path.read_text(encoding="utf-8")
    assert "FMP_API_KEY" not in os.environ


def test_update_preserves_comments_blank_lines_and_unrelated_order(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# top comment\nSOME_OTHER_VAR=keep-me\n\nFMP_API_KEY=old\n",
        encoding="utf-8",
    )
    service = IntegrationConfigService(env_path)

    service.update({"FMP_API_KEY": "new"})

    content = env_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "# top comment"
    assert "SOME_OTHER_VAR=keep-me" in lines
    assert "" in lines
    assert "FMP_API_KEY=new" in lines
    assert "FMP_API_KEY=old" not in content


def test_status_strips_trailing_inline_comment_from_an_unquoted_value(
    tmp_path,
) -> None:
    """Regression: EMAIL_PORT=587  # STARTTLS must parse as "587", not the
    whole comment — an earlier bug let the comment leak into the parsed
    value, which then failed EMAIL_PORT's numeric validation on save."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "EMAIL_HOST=smtp.gmail.com        # change for Outlook:\n"
        "EMAIL_PORT=587                    # 587 = STARTTLS; 465 = SSL\n",
        encoding="utf-8",
    )
    service = IntegrationConfigService(env_path)

    status = service.status()

    assert status["EMAIL_HOST"]["value"] == "smtp.gmail.com"
    assert status["EMAIL_PORT"]["value"] == "587"


def test_status_preserves_hash_inside_a_quoted_value(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        'EMAIL_HOST="smtp.example.com # not a comment"\n', encoding="utf-8"
    )
    service = IntegrationConfigService(env_path)

    assert service.status()["EMAIL_HOST"]["value"] == "smtp.example.com # not a comment"


def test_value_starting_with_hash_and_no_content_parses_as_blank(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EMAIL_HOST=# just a comment\n", encoding="utf-8")
    service = IntegrationConfigService(env_path)

    assert service.status()["EMAIL_HOST"]["configured"] is False


def test_malformed_line_is_preserved_without_being_parsed(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("this line has no equals sign\n", encoding="utf-8")
    service = IntegrationConfigService(env_path)

    service.update({"FMP_API_KEY": "sk-123"})

    content = env_path.read_text(encoding="utf-8")
    assert "this line has no equals sign" in content


def test_write_sets_mode_0600(tmp_path) -> None:
    env_path = tmp_path / ".env"
    service = IntegrationConfigService(env_path)
    service.update({"FMP_API_KEY": "sk-123"})

    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_symlinked_env_path_is_rejected(tmp_path) -> None:
    real_target = tmp_path / "elsewhere.env"
    real_target.write_text("", encoding="utf-8")
    symlink_path = tmp_path / ".env"
    symlink_path.symlink_to(real_target)

    with pytest.raises(InvalidEnvPathError):
        IntegrationConfigService(symlink_path)


def test_write_failure_leaves_original_file_and_environ_untouched(
    tmp_path, monkeypatch
) -> None:
    env_path = tmp_path / ".env"
    service = IntegrationConfigService(env_path)
    service.update({"FMP_API_KEY": "sk-original"})

    import app.services.integration_config_service as module

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "fsync", _boom)

    with pytest.raises(OSError):
        service.update({"FMP_API_KEY": "sk-should-not-land"})

    assert "sk-original" in env_path.read_text(encoding="utf-8")
    assert "sk-should-not-land" not in env_path.read_text(encoding="utf-8")
    assert os.environ["FMP_API_KEY"] == "sk-original"
    # No leftover temp files from the failed write.
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_concurrent_updates_do_not_lose_unrelated_keys(tmp_path) -> None:
    env_path = tmp_path / ".env"
    service = IntegrationConfigService(env_path)
    service.update({"EMAIL_HOST": "smtp.example.com"})

    def _write(key: str, value: str) -> None:
        IntegrationConfigService(env_path).update({key: value})

    threads = [
        threading.Thread(target=_write, args=("FMP_API_KEY", "fmp-val")),
        threading.Thread(target=_write, args=("ALPHA_VANTAGE_API_KEY", "av-val")),
        threading.Thread(target=_write, args=("EMAIL_PORT", "587")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    status = IntegrationConfigService(env_path).status()
    assert status["FMP_API_KEY"]["configured"] is True
    assert status["ALPHA_VANTAGE_API_KEY"]["configured"] is True
    assert status["EMAIL_PORT"]["value"] == "587"
    assert status["EMAIL_HOST"]["value"] == "smtp.example.com"


def test_invalid_email_port_is_rejected(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    with pytest.raises(Exception):  # noqa: B017 - InvalidFieldValueError
        service.update({"EMAIL_PORT": "not-a-number"})
    with pytest.raises(Exception):  # noqa: B017
        service.update({"EMAIL_PORT": "99999"})


def test_rotate_token_generates_high_entropy_urlsafe_token(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    token = service.rotate_token()

    assert len(token) >= 32
    assert service.status()["APP_AUTH_TOKEN"]["configured"] is True
    assert "value" not in service.status()["APP_AUTH_TOKEN"]
    assert os.environ["APP_AUTH_TOKEN"] == token


def test_rotate_token_invalidates_the_previous_token(tmp_path) -> None:
    service = IntegrationConfigService(tmp_path / ".env")
    first = service.rotate_token()
    second = service.rotate_token()

    assert first != second
    assert os.environ["APP_AUTH_TOKEN"] == second


def test_saved_key_is_inherited_by_a_subprocess_without_exposing_it_on_the_cli(
    tmp_path,
) -> None:
    """A saved key updates os.environ, so a launched pipeline subprocess sees it."""
    service = IntegrationConfigService(tmp_path / ".env")
    service.update({"FMP_API_KEY": "sk-inherited"})

    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('FMP_API_KEY', ''))"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "sk-inherited"
