"""Point-in-time S&P 500 membership import, store and checks (#68)."""

from pathlib import Path

import pytest

from app.cli import import_index_membership as cli
from app.repositories import db
from app.repositories.index_membership_repo import IndexMembershipRepository
from app.services.index_membership import sp500_import as sp500
from app.services.index_membership import wikipedia_check as wiki

SHA = "abc123"

# AAL leaves 1997 and is reused 2015; OLD is renamed NEW in 2001; NEW leaves
# in 2005 and returns in 2008. Rows deliberately out of order.
COMPONENTS = """date,tickers
2001-01-16,"KEEP,NEW"
1996-01-02,"AAL,OLD,KEEP,KEEP"
1997-01-15,"KEEP,OLD"
2005-03-01,"KEEP"
2008-06-02,"KEEP,NEW"
2015-03-23,"AAL,KEEP,NEW"
"""

START_END = """ticker,start_date,end_date
AAL,1996-01-02,1997-01-15
AAL,2015-03-23,
KEEP,1996-01-02,
NEW,2001-01-16,2005-03-01
NEW,2008-06-02,
OLD,1996-01-02,2001-01-17
"""

EXPECTED = [
    ("AAL", "1996-01-02", "1997-01-15", "low"),
    ("AAL", "2015-03-23", None, "normal"),
    ("KEEP", "1996-01-02", None, "low"),
    ("NEW", "2001-01-16", "2005-03-01", "normal"),
    ("NEW", "2008-06-02", None, "normal"),
    ("OLD", "1996-01-02", "2001-01-16", "low"),
]

CONSTITUENTS_HTML = """<table id="other"><tr><td>X</td></tr></table>
<table class="wikitable" id="constituents">
<tr><th>Symbol</th><th>Security</th></tr>
<tr><td><a>AAL</a></td><td>American Airlines</td></tr>
<tr><td>KEEP</td><td>Keep Co<sup>[1]</sup><table><tr><td>Z</td></tr></table></td></tr>
<tr><td>BRK.B</td><td>Berkshire</td></tr>
</table>"""

CHANGES_HTML = """<table class="wikitable sortable" id="changes">
<tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th></tr>
<tr><th>Ticker</th><th>Security</th></tr>
<tr><td>March 1, 2016</td><td>BRK.B</td><td>Berkshire</td><td>NEW</td>
<td>New Co</td><td>Market cap.</td><td><sup>[2]</sup></td></tr>
<tr><td>March 23, 2015</td><td>AAL</td><td>American</td><td>X</td>
<td>X Co</td><td>Same day as dataset end.</td><td></td></tr>
<tr><td>June 30, 2016</td><td></td><td></td><td>OLD2</td>
<td>Old</td><td>Removed only.</td><td></td></tr>
</table>"""


def _fetcher(files: dict[str, bytes]) -> sp500.Fetch:
    return files.__getitem__


def _source(components: str = COMPONENTS) -> dict[str, bytes]:
    return {
        sp500.COMMIT_API_URL: f'{{"sha": "{SHA}"}}'.encode(),
        sp500.raw_url(SHA, sp500.COMPONENTS_FILE): components.encode(),
        sp500.raw_url(SHA, sp500.START_END_FILE): START_END.encode(),
        wiki.CONSTITUENTS_URL: CONSTITUENTS_HTML.encode(),
        wiki.CHANGES_URL: CHANGES_HTML.encode(),
    }


@pytest.fixture
def repo(tmp_path: Path) -> IndexMembershipRepository:
    repo = IndexMembershipRepository(db.make_connect(lambda: tmp_path / "m.db"))
    repo.ensure_schema()
    repo.ensure_schema()  # idempotent
    return repo


def test_parse_sorts_rows_and_dedupes_tickers() -> None:
    snapshots = sp500.parse_components(COMPONENTS)
    assert [d for d, _ in snapshots][:2] == ["1996-01-02", "1997-01-15"]
    assert snapshots[0][1] == {"AAL", "OLD", "KEEP"}
    assert sp500.member_counts(snapshots)["2005-03-01"] == 1


def test_parse_rejects_empty_file() -> None:
    with pytest.raises(ValueError):
        sp500.parse_components("date,tickers\n")


def test_intervals_split_reused_renamed_and_returning_tickers() -> None:
    intervals = sp500.derive_intervals(sp500.parse_components(COMPONENTS))
    got = [(i.ticker, i.start_date, i.end_date, i.confidence) for i in intervals]
    assert got == EXPECTED
    keys = {i.security_key for i in intervals}
    assert {"AAL@1996-01-02", "AAL@2015-03-23"} <= keys  # reused: distinct
    assert len(keys) == len(intervals)


def test_confidence_cutoff() -> None:
    assert sp500.confidence_on("2001-01-15") == "low"
    assert sp500.confidence_on("2001-01-16") == "normal"


def test_cross_check_reports_both_sides() -> None:
    intervals = sp500.derive_intervals(sp500.parse_components(COMPONENTS))
    only_derived, only_file = sp500.cross_check(
        intervals, sp500.parse_start_end(START_END)
    )
    assert only_derived == [("OLD", "1996-01-02", "2001-01-16")]
    assert only_file == [("OLD", "1996-01-02", "2001-01-17")]


def test_members_on_is_start_inclusive_end_exclusive(
    repo: IndexMembershipRepository,
) -> None:
    sp500.import_sp500(_fetcher(_source()), repo)

    def on(as_of: str) -> list[str]:
        return [i.security_key for i in repo.members_on("sp500", as_of)]

    assert on("1995-12-29") == []
    assert on("1996-01-02") == ["AAL@1996-01-02", "KEEP@1996-01-02", "OLD@1996-01-02"]
    assert "AAL@1996-01-02" in on("1997-01-14")
    assert on("1997-01-15") == ["KEEP@1996-01-02", "OLD@1996-01-02"]
    assert on("2001-01-16") == ["KEEP@1996-01-02", "NEW@2001-01-16"]
    assert on("2030-01-01") == ["AAL@2015-03-23", "KEEP@1996-01-02", "NEW@2008-06-02"]
    assert [i.security_key for i in repo.intervals_for("sp500", "NEW")] == [
        "NEW@2001-01-16",
        "NEW@2008-06-02",
    ]
    assert repo.members_on("ftse100", "2010-01-04") == []


def test_reimport_is_idempotent_and_reads_use_latest(
    repo: IndexMembershipRepository,
) -> None:
    first = sp500.import_sp500(_fetcher(_source()), repo)
    again = sp500.import_sp500(_fetcher(_source()), repo)
    assert first.created and not again.created
    assert again.import_id == first.import_id

    updated = COMPONENTS + '2020-01-02,"KEEP,NEW"\n'
    third = sp500.import_sp500(_fetcher(_source(updated)), repo)
    latest = repo.latest_import("sp500")
    assert third.created and latest is not None and latest.id == third.import_id
    assert latest.source_ref == f"fja05680/sp500@{SHA}"
    assert latest.last_date == "2020-01-02" and latest.interval_count == 6
    assert [i.ticker for i in repo.members_on("sp500", "2020-01-02")] == [
        "KEEP",
        "NEW",
    ]


def test_dry_run_writes_nothing_and_unpinned_falls_back_to_branch() -> None:
    files = _source()
    del files[sp500.COMMIT_API_URL]
    files[sp500.raw_url("master", sp500.COMPONENTS_FILE)] = COMPONENTS.encode()
    files[sp500.raw_url("master", sp500.START_END_FILE)] = START_END.encode()
    summary = sp500.import_sp500(_fetcher(files), None)
    assert summary.source_ref == "fja05680/sp500@master"
    assert summary.import_id is None and not summary.created
    assert summary.interval_count == 6 and summary.low_confidence_intervals == 3
    assert summary.latest_members == {"AAL", "KEEP", "NEW"}


def test_wikipedia_diff_reports_members_and_later_changes() -> None:
    diff = wiki.fetch_wikipedia_diff(
        _fetcher(_source()), frozenset({"AAL", "KEEP", "NEW"}), "2015-03-23"
    )
    assert diff.only_in_dataset == ["NEW"]
    assert diff.only_in_wikipedia == ["BRK.B"]
    assert [(c.date, c.added, c.removed) for c in diff.changes_after] == [
        ("2016-03-01", "BRK.B", "NEW"),
        ("2016-06-30", None, "OLD2"),
    ]
    assert diff.changes_after[0].reason == "Market cap."


def test_table_rows_skips_headers_nested_tables_and_footnotes() -> None:
    assert wiki.table_rows(CONSTITUENTS_HTML, "constituents") == [
        ["AAL", "American Airlines"],
        ["KEEP", "Keep Co"],
        ["BRK.B", "Berkshire"],
    ]


def test_cli_imports_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "data" / "index_membership.db"
    monkeypatch.setattr(cli, "INDEX_MEMBERSHIP_DB", db_path)
    cli.main(["sp500", "--dry-run"], fetch=_fetcher(_source()))
    assert not db_path.exists()
    cli.main(["sp500", "--wikipedia"], fetch=_fetcher(_source()))
    out = capsys.readouterr().out
    assert "interval_count: 6" in out and "created: True" in out
    assert "only_in_start_end: 1" in out
    assert "wikipedia only_in_wikipedia: ['BRK.B']" in out
