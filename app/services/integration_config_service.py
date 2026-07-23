"""Integration configuration service — safely reads/writes secrets in ``.env``.

Backs the Settings/Integrations UI (#47). Only an explicit allowlist of keys
can ever be read or written; nothing here accepts an arbitrary environment
variable name. Writes are atomic (temp file + ``os.replace``) and update the
live ``os.environ`` so a subsequently launched pipeline subprocess inherits
the new value without a server restart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
import secrets
import stat
import tempfile
import threading
from pathlib import Path
from typing import Final

from app.core.config import ROOT_DIR

ENV_PATH_DEFAULT: Final[Path] = ROOT_DIR / ".env"

# The only environment variables this service will ever read or write.
ALLOWED_KEYS: Final[tuple[str, ...]] = (
    "FMP_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "EMAIL_USER",
    "EMAIL_PASSWORD",
    "EMAIL_TO",
    "EMAIL_HOST",
    "EMAIL_PORT",
    "APP_AUTH_TOKEN",
)

# EMAIL_HOST/EMAIL_PORT are safe to echo back to the browser (not secrets).
# Every other allowed key only ever reports Configured/Missing.
DISPLAYABLE_KEYS: Final[frozenset[str]] = frozenset({"EMAIL_HOST", "EMAIL_PORT"})

_write_lock = threading.Lock()


class IntegrationConfigError(Exception):
    """Base error for integration configuration failures."""


class UnknownConfigKeyError(IntegrationConfigError):
    """Raised when a caller references a key outside ``ALLOWED_KEYS``."""


class InvalidEnvPathError(IntegrationConfigError):
    """Raised when the configured ``.env`` path is unsafe to write to."""


class InvalidFieldValueError(IntegrationConfigError):
    """Raised when a submitted field value fails basic validation."""


def _split_lines(content: str) -> list[str]:
    return content.splitlines()


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return _split_lines(path.read_text(encoding="utf-8"))


def _line_key(line: str) -> str | None:
    """Return the variable name a ``.env`` line assigns, if any.

    Blank lines, comments, and anything without an ``=`` are not
    assignments and are preserved verbatim rather than parsed.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key or None


def _strip_inline_comment(raw: str) -> str:
    """Drop a trailing ``# comment`` from an unquoted ``.env`` value.

    Matches the common convention (and this repo's own ``.env``) of
    annotating a value on the same line, e.g. ``EMAIL_PORT=587  # STARTTLS``
    — without this, the parsed value would include the comment text.
    """
    if raw.startswith("#"):
        return ""
    for marker in (" #", "\t#"):
        index = raw.find(marker)
        if index != -1:
            raw = raw[:index]
    return raw


def _line_value(line: str) -> str:
    raw = line.split("=", 1)[1].strip()
    if raw[:1] in ('"', "'"):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
        return raw[1:]
    return _strip_inline_comment(raw).strip()


def _serialize_value(value: str) -> str:
    if any(char in value for char in " \t#\"'\\"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def read_all(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file into a flat key/value mapping.

    Unrelated formatting (comments, blank lines, ordering) is not part of
    this view — it is only used to answer "what is the current value of
    key X", never to round-trip the file.
    """
    values: dict[str, str] = {}
    for line in _read_lines(path):
        key = _line_key(line)
        if key is not None:
            values[key] = _line_value(line)
    return values


def _render_lines(
    lines: list[str], updates: Mapping[str, str], clear_keys: set[str]
) -> list[str]:
    """Merge ``updates``/``clear_keys`` into ``lines``, preserving the rest.

    Existing assignments are replaced in place; unrecognised/malformed
    lines and comments pass through untouched. New keys are appended.
    """
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        key = _line_key(line)
        if key is None:
            output.append(line)
            continue
        if key in clear_keys:
            continue
        if key in remaining:
            output.append(f"{key}={_serialize_value(remaining.pop(key))}")
            continue
        output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={_serialize_value(value)}")
    return output


def _atomic_write(path: Path, lines: list[str]) -> None:
    """Write ``lines`` to ``path`` atomically, mode 0600.

    Writes to a same-directory temp file, flushes/fsyncs, restricts
    permissions, then ``os.replace``s it into place — a crash or failure
    at any point before the final replace leaves the original untouched.
    """
    content = "\n".join(lines)
    if content:
        content += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _validate_field(key: str, value: str) -> None:
    if key == "EMAIL_PORT":
        if not value.isdigit() or not (1 <= int(value) <= 65535):
            raise InvalidFieldValueError(
                "EMAIL_PORT must be a number between 1 and 65535"
            )
    elif not value.strip():
        raise InvalidFieldValueError(f"{key} may not be blank")


class IntegrationConfigService:
    """Reads and writes the allowlisted integration settings in ``.env``."""

    def __init__(self, env_path: Path | None = None) -> None:
        # The repo-boundary check only applies to the default path: it
        # guards against the *production* `.env` being silently redirected
        # (e.g. by a symlinked ancestor directory) outside the repo. An
        # explicitly injected path is a deliberate caller decision — tests
        # rely on injecting a path under a pytest tmp_path, which normally
        # lives outside the repo.
        self._enforce_repo_boundary = env_path is None
        self.env_path = env_path if env_path is not None else ENV_PATH_DEFAULT
        self._validate_env_path()

    def _validate_env_path(self) -> None:
        if self.env_path.is_symlink():
            raise InvalidEnvPathError(f"{self.env_path} is a symlink")
        if not self._enforce_repo_boundary:
            return
        root = ROOT_DIR.resolve()
        parent = self.env_path.parent.resolve()
        if parent != root and root not in parent.parents:
            raise InvalidEnvPathError(f"{self.env_path} is outside the project root")

    def status(self) -> dict[str, dict[str, object]]:
        """Return, per allowed key, whether it's configured and any safe value.

        Secret keys never report their value — only ``configured``. The two
        keys in ``DISPLAYABLE_KEYS`` also report their current value.
        """
        current = read_all(self.env_path)
        result: dict[str, dict[str, object]] = {}
        for key in ALLOWED_KEYS:
            value = current.get(key) or os.getenv(key)
            entry: dict[str, object] = {"configured": bool(value)}
            if key in DISPLAYABLE_KEYS and value:
                entry["value"] = value
            result[key] = entry
        return result

    def update(self, values: Mapping[str, str], clear_keys: Iterable[str] = ()) -> None:
        """Apply ``values`` and remove ``clear_keys``, atomically.

        Blank values in ``values`` are ignored (blank means "leave
        unchanged") — clearing a key requires listing it in ``clear_keys``.
        """
        clear_keys = set(clear_keys)
        unknown = (set(values) | clear_keys) - set(ALLOWED_KEYS)
        if unknown:
            raise UnknownConfigKeyError(
                f"Unknown configuration keys: {sorted(unknown)}"
            )
        to_write = {key: value for key, value in values.items() if value.strip()}
        for key, value in to_write.items():
            _validate_field(key, value)

        with _write_lock:
            self._ensure_gitignored()
            lines = _read_lines(self.env_path)
            new_lines = _render_lines(lines, to_write, clear_keys)
            _atomic_write(self.env_path, new_lines)
            for key, value in to_write.items():
                os.environ[key] = value
            for key in clear_keys:
                os.environ.pop(key, None)

    def rotate_token(self) -> str:
        """Generate and persist a new ``APP_AUTH_TOKEN``, returning it once.

        The caller is responsible for showing this value to the user
        exactly once — it is not recoverable afterwards from this service.
        """
        token = secrets.token_urlsafe(32)
        self.update({"APP_AUTH_TOKEN": token})
        return token

    def _ensure_gitignored(self) -> None:
        gitignore = ROOT_DIR / ".gitignore"
        entry = self.env_path.name if self.env_path.parent == ROOT_DIR else None
        if entry is None:
            return
        try:
            content = (
                gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            )
        except OSError:
            return
        if entry in {line.strip() for line in content.splitlines()}:
            return
        new_content = (
            content if content.endswith("\n") or not content else content + "\n"
        )
        new_content += f"{entry}\n"
        try:
            gitignore.write_text(new_content, encoding="utf-8")
        except OSError:
            pass
