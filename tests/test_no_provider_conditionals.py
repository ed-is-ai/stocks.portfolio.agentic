"""Architecture guard for AC4 (Story 3.4): no shared import-path source
file may contain a hardcoded ``"ig"``-shaped provider-identity comparison.

Every one of the three Story 3.4 capabilities (text extraction,
signed-amount splitting, the cash-balance fallback) is keyed only on the
*presence* of a capability on the loaded contract (e.g.
``contract.text_extraction is not None``), never on
``contract.provider_id``. This is a simple, regex-based grep-in-test --
deliberately not a full AST analysis (see the spec's Deferred Work) -- so
it proves AC4 by construction rather than by convention alone, catching
the most obvious regression (a literal ``provider_id == "ig"`` branch)
without the maintenance cost of a real static-analysis pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent

#: Every shared file a provider-specific conditional would be a red flag
#: in -- each is exercised by both II and IG today, so any of them
#: special-casing one provider's id would violate AC4.
_GUARDED_FILES = [
    "app/agents/trader/trader_agent.py",
    "app/api/routes/portfolio.py",
    "app/services/portfolio_import/normalizer.py",
    "app/services/portfolio_import/contract_registry.py",
    "app/services/portfolio_service.py",
]

#: Matches a provider-identity comparison against the literal ``"ig"``, in
#: either quoting style, case-insensitively, on either side of ``==``/
#: ``!=`` -- e.g. ``provider_id == "ig"``, ``'IG' == provider_id``,
#: ``self.provider_id != "Ig"``. Deliberately narrow (a literal-string
#: comparison), not a full identifier/AST analysis.
_PROVIDER_ID_COMPARISON_RE = re.compile(
    r"""
    (?:
        provider_id \s* (?:==|!=) \s* ['"]ig['"]
        |
        ['"]ig['"] \s* (?:==|!=) \s* provider_id
        |
        provider_id \s+ (?:not \s+)? in \s* \( [^)]*['"]ig['"][^)]* \)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@pytest.mark.parametrize("relative_path", _GUARDED_FILES)
def test_file_has_no_ig_provider_identity_comparison(relative_path: str) -> None:
    source = (_ROOT / relative_path).read_text(encoding="utf-8")
    match = _PROVIDER_ID_COMPARISON_RE.search(source)
    assert match is None, (
        f"{relative_path} contains an IG-specific provider_id comparison "
        f"({match.group(0)!r} if not None) -- Story 3.4's capabilities must "
        "be keyed only on a contract's own declared fields (e.g. "
        "`contract.text_extraction is not None`), never on `provider_id`."
    )


def test_guarded_files_all_exist() -> None:
    """Sanity check the guard itself isn't silently vacuous against a
    renamed/moved file (which would make ``read_text`` raise, but this
    makes the intent explicit and gives a clearer failure message)."""
    missing = [path for path in _GUARDED_FILES if not (_ROOT / path).is_file()]
    assert missing == []


def test_regex_actually_catches_a_provider_specific_comparison() -> None:
    """Guard the guard: a synthetic snippet shaped exactly like the
    violation this test exists to catch must actually match."""
    assert _PROVIDER_ID_COMPARISON_RE.search('if contract.provider_id == "ig":')
    assert _PROVIDER_ID_COMPARISON_RE.search("if provider_id == 'IG':")
    assert _PROVIDER_ID_COMPARISON_RE.search('if "ig" == provider_id:')
    assert _PROVIDER_ID_COMPARISON_RE.search('if provider_id in ("ig", "other"):')
    assert not _PROVIDER_ID_COMPARISON_RE.search(
        'if contract.provider_id == "interactive_investor":'
    )
