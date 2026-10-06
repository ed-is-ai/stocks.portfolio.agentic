"""SEC EDGAR client and pure parsers for terminal events (#73).

SEC asks for a descriptive ``User-Agent`` and at most 10 requests per second;
``sec_fetch`` enforces both and retries throttling and transient errors.
Everything else here is a pure parser so tests run on fixtures.
"""

import html
import json
import re
import time
import unicodedata
from collections.abc import Callable

import requests
from pydantic import BaseModel, ConfigDict

from app.services.index_membership.sp500_import import Fetch

CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
#: Seconds between requests: just under SEC's 10 requests/second.
MIN_INTERVAL = 0.11
#: Pauses before each retry of a throttled (429) or failed (5xx) request.
RETRY_WAITS = (2.0, 10.0)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_SUFFIXES = frozenset("INC CORP CORPORATION CO COMPANY LTD PLC HOLDINGS THE".split())
_STATE = re.compile(r"[/\\][A-Z]{2}[/\\]")  # EDGAR's "/DE/" state tags
_AMOUNT = r"\$\s*(\d[\d,]*(?:\.\d+)?)"
_CASH_PRICE = re.compile(
    rf"{_AMOUNT}\s+per\s+share\s+in\s+cash"
    rf"|{_AMOUNT}\s+in\s+cash\s+per\s+share"
    rf"|cash\s+consideration\s+of\s+{_AMOUNT}\s+per\s+share",
    re.IGNORECASE,
)
#: Shares offered alongside the cash, e.g. "$20.00 in cash and 0.5 shares".
_PLUS_STOCK = re.compile(
    r"\W*and\s+(?:\d+(?:\.\d+)?|one)\s+(?:\w+\s+){0,3}?shares?\b", re.I
)


class Filing(BaseModel):
    """One EDGAR filing from a company's submissions history."""

    model_config = ConfigDict(frozen=True)

    form: str
    date: str
    items: tuple[str, ...]
    url: str


def sec_fetch(
    user_agent: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Fetch:
    """Return a ``Fetch`` for SEC hosts, spacing requests by ``MIN_INTERVAL``
    and retrying 429/5xx responses and connection errors after ``RETRY_WAITS``.

    ``sleep`` and ``clock`` are injectable so tests never wait.
    """
    last = [float("-inf")]

    def get(url: str) -> requests.Response:
        wait = last[0] + MIN_INTERVAL - clock()
        if wait > 0:
            sleep(wait)
        last[0] = clock()
        return requests.get(url, timeout=30, headers={"User-Agent": user_agent})

    def fetch(url: str) -> bytes:
        for pause in RETRY_WAITS:
            try:
                response = get(url)
            except (requests.ConnectionError, requests.Timeout):
                sleep(pause)
                continue
            if response.status_code not in _RETRY_STATUS:
                break
            sleep(pause)
        else:
            response = get(url)
        response.raise_for_status()
        return response.content

    return fetch


def normalise_name(name: str) -> str:
    """Upper-case ``name``, fold accents, spell ``&`` as ``AND``, and drop
    punctuation, state tags and legal suffixes."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = _STATE.sub(" ", folded.upper()).replace(".", "").replace("'", "")
    text = text.replace("&", " AND ")
    words = re.sub(r"[^A-Z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def parse_cik_lookup(text: str) -> dict[str, set[int]]:
    """Map normalised names to CIKs from ``cik-lookup-data.txt`` (``NAME:CIK:``)."""
    lookup: dict[str, set[int]] = {}
    for line in text.splitlines():
        name, _, cik = line.rstrip().rstrip(":").rpartition(":")
        if name and cik.isdigit():
            lookup.setdefault(normalise_name(name), set()).add(int(cik))
    return lookup


def parse_filings(
    submissions_json: bytes | str, cik: int | None = None
) -> list[Filing]:
    """Return the filings of a submissions JSON: its ``filings.recent`` block,
    or, for an older-history page (which has no ``cik``), the page itself."""
    data = json.loads(submissions_json)
    cik = int(data["cik"]) if cik is None else cik
    block = data["filings"]["recent"] if "filings" in data else data
    columns = zip(
        block["form"],
        block["filingDate"],
        block["items"],
        block["accessionNumber"],
        block["primaryDocument"],
        strict=True,
    )
    return [
        Filing(
            form=form,
            date=filed,
            items=tuple(i.strip() for i in (items or "").split(",") if i.strip()),
            url=ARCHIVE_URL.format(
                cik=cik, accession=accession.replace("-", ""), doc=doc
            ),
        )
        for form, filed, items, accession, doc in columns
    ]


def older_pages(submissions_json: bytes | str, first: str, last: str) -> list[str]:
    """Return the URLs of a submissions JSON's older-history pages that
    overlap ``[first, last]`` (``filings.recent`` holds only ~1000 filings)."""
    pages = json.loads(submissions_json)["filings"].get("files", [])
    return [
        SUBMISSIONS_PAGE_URL.format(name=page["name"])
        for page in pages
        if page["filingFrom"] <= last and page["filingTo"] >= first
    ]


def cash_price_per_share(text: str) -> tuple[float, bool] | None:
    """Return the first cash-per-share price in an (HTML) merger document and
    whether shares are offered with it (a mixed cash-and-stock deal)."""
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    match = _CASH_PRICE.search(plain)
    if match is None:
        return None
    amount = next(g for g in match.groups() if g is not None)
    mixed = _PLUS_STOCK.match(plain, match.end()) is not None
    return float(amount.replace(",", "")), mixed
