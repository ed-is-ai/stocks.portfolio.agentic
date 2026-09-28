"""Read-only, anonymised evidence for the Research Copilot (GH-13).

Pure builders: one analysis record plus the artifact's run identity,
freshness and source health become numbered ``E1…En`` evidence items and
explicit limitations. Everything here is safe to send to the model:

* the security is only ever called ``LABEL`` — the (upper-case) ticker and
  its root symbol are replaced case-sensitively, and a ticker that is also
  an ordinary word (``A``, ``ON``, ``IT``…) only where it cannot be read as
  that word;
* absolute price levels are sent only as percent distance from the latest
  close; any text carrying a currency amount, and any free text carrying
  one of the record's price levels or a two-decimal number, is withheld
  (and the withholding itself becomes a limitation);
* non-finite numbers are omitted; volume, price history and OHLCV are
  never read.

``reveal`` maps the label back to the real ticker for local display only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import math
import re
from typing import NamedTuple

from app.core.recommendation import (
    AVOID_SCORE_MAX,
    BUY_SCORE_MIN,
    STAGE_2,
    STAY_ALERT_SCORE_MIN,
    Recommendation,
    classify_recommendation,
)
from app.schemas.analysis_artifact import AnalysisArtifactMeta
from app.schemas.evidence_ref import EvidenceRefV1
from app.schemas.record import StockRecord
from app.schemas.scan import StockAnalysis
from app.schemas.source_health import SourceHealth, SourceName, SourceState
from app.services.freshness_service import Freshness, FreshnessState

LABEL = "Security A"
UNKNOWN_RUN_ID = "unknown"
LIMITATION_KIND = "limitation"
AMOUNT = "[amount]"

_ANALYSIS_SOURCE = "analysis artifact"
_RULES_SOURCE = "recommendation rules"
_FREE_TEXT_KINDS = frozenset({"strength", "risk", "summary"})
_LABEL_PATTERN = re.compile(rf"\b{re.escape(LABEL)}\b", re.IGNORECASE)
_NUM = r"\d(?:,?\d)*(?:\.\d+)?"
_CURRENCY_WORDS = r"(?i:USD|GBP|EUR|GBX|pence|pounds?|dollars?|euros?)"
# "£12", "$ 1,234.50", "USD 150", "150 GBP", "12 pence", "250p" / "250 GBp".
CURRENCY_AMOUNT = re.compile(
    rf"[£$€]\s?{_NUM}"
    rf"|\b{_CURRENCY_WORDS}\s?{_NUM}"
    rf"|{_NUM}\s?(?:{_CURRENCY_WORDS}|p)\b"
)
# Any bare number, and a two-decimal one that is not a percentage or ratio.
_NUMBER = re.compile(rf"(?<![\d.,]){_NUM}(?!\d)")
_TWO_DP = re.compile(r"(?<![\d.,])\d(?:,?\d)*\.\d{2}(?!\d)(?!\.\d)(?!\s?[%x])")
# Ticker roots that are also ordinary English words (plus any single letter).
_COMMON_WORDS = frozenset(
    "A I ON IT ALL NOW KEY SO BE GO AN AT BY OR ARE CAN FOR NEW ONE OUT SEE TWO "
    "WELL BIG CAR FUN HAS RUN TRUE REAL".split()
)
_WITHHELD = (
    "{} evidence item(s) withheld because they contained price or currency amounts."
)
_SOURCE_GAPS = {
    SourceState.FAILED: "failed",
    SourceState.SKIPPED: "was skipped",
    SourceState.EMPTY: "returned no data",
}


@dataclass(frozen=True)
class EvidenceItem:
    """One numbered fact supplied to the copilot, with its citable ref."""

    ref: EvidenceRefV1
    text: str
    limitation: bool = False


class _Fact(NamedTuple):
    kind: str
    text: str
    as_of: date | None
    source: str = _ANALYSIS_SOURCE


def build_evidence(
    record: StockRecord,
    *,
    is_held: bool,
    meta: AnalysisArtifactMeta | None,
    freshness: Freshness,
    source_health: Mapping[SourceName, SourceHealth],
) -> list[EvidenceItem]:
    """Return the anonymised, numbered evidence for one record.

    Limitations (stale/unknown freshness, unknown run, failed, skipped,
    empty or cached sources, a missing analysis section, withheld items)
    come last, as items of kind ``LIMITATION_KIND`` so they can be cited
    and surfaced as unknowns.
    """
    tokens = _price_tokens(record)
    facts = _record_facts(record, is_held, _parse_date(record.as_of))
    kept = [fact for fact in facts if not _leaks_amount(fact, tokens)]
    limits = _limitations(record, meta, freshness, source_health)
    if withheld := len(facts) - len(kept):
        limits.append(_limit(_WITHHELD.format(withheld)))
    return [
        EvidenceItem(
            EvidenceRefV1(
                kind=fact.kind, id=f"E{n}", as_of=fact.as_of, source=fact.source
            ),
            anonymise(fact.text, record.ticker),
            fact.kind == LIMITATION_KIND,
        )
        for n, fact in enumerate([*kept, *limits], start=1)
    ]


def anonymise(text: str, ticker: str, *, question: bool = False) -> str:
    """Replace the ticker with ``LABEL`` wherever it is not an ordinary word.

    Matches already inside ``LABEL`` are left alone, so ticker ``A`` never
    turns "Security A" into "Security Security A". In a user's question an
    upper-case match is the ticker ("Why is ON rated Buy?"): only a
    single-letter one that opens a sentence is kept, since over-redacting a
    question is harmless and leaking the ticker is not.
    """
    labels = [m.span() for m in _LABEL_PATTERN.finditer(text)]

    def swap(match: re.Match[str]) -> str:
        if any(start <= match.start() < end for start, end in labels):
            return match.group()
        if question:
            keep = len(match.group().lstrip("^")) == 1 and _opens_sentence(text, match)
        else:
            keep = _is_common_word(match.group()) and _reads_as_word(text, match)
        return match.group() if keep else LABEL

    return _ticker_pattern(ticker).sub(swap, text)


def anonymise_question(question: str, record: StockRecord) -> str:
    """Anonymise a user question, redacting (not dropping) any amounts."""
    tokens = _price_tokens(record)
    redacted = _TWO_DP.sub(AMOUNT, CURRENCY_AMOUNT.sub(AMOUNT, question))
    redacted = _NUMBER.sub(
        lambda m: AMOUNT if m.group().replace(",", "") in tokens else m.group(),
        redacted,
    )
    return anonymise(redacted, record.ticker, question=True)


def reveal(text: str, ticker: str) -> str:
    """Map ``LABEL`` back to the real ticker, for local display only."""
    return _LABEL_PATTERN.sub(ticker, text)


def recommendation_reason(record: StockRecord, rec: Recommendation) -> str:
    """Explain *rec* from the same rules and thresholds that produced it."""
    a = record.analysis
    if a is None:
        return "no analysis is recorded"
    if rec.bucket in ("sell", "avoid"):
        why = (
            f"score is at or below {AVOID_SCORE_MAX}"
            if a.score <= AVOID_SCORE_MAX
            else f"{a.stage} is a topping or declining trend"
        )
    elif rec.bucket in ("buy", "buy_lowvol"):
        volume = "volume confirmed" if a.volume_confirmed else "volume not confirmed"
        why = f"{STAGE_2} breakout with score at least {BUY_SCORE_MIN}, {volume}"
    elif rec.bucket == "extended":
        why = f"{STAGE_2} but extended past the entry zone"
    elif rec.bucket == "alert":
        why = f"{STAGE_2} approaching entry with score at least {STAY_ALERT_SCORE_MIN}"
    elif rec.bucket == "watch":
        why = f"{STAGE_2} getting close to entry"
    else:
        why = (
            f"no rule matched (Buy needs {STAGE_2}, broken_out and score at "
            f"least {BUY_SCORE_MIN}; Stay Alert needs {STAGE_2}, approaching "
            f"and score at least {STAY_ALERT_SCORE_MIN})"
        )
    return f"{a.stage}, entry zone {a.entry_zone}, score {a.score}/10: {why}"


def _ticker_pattern(ticker: str) -> re.Pattern[str]:
    """Match the ticker or its root (``BARC`` of ``BARC.L``, ``FTSE`` of ``^FTSE``).

    Lookarounds rather than ``\\b`` so tickers starting or ending in a
    non-word character (``^FTSE``, ``BRK.B``) still match.
    """
    root = ticker.split(".")[0]
    names = sorted({ticker, root, root.lstrip("^")} - {""}, key=len, reverse=True)
    alternatives = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?<![\w.^])(?:{alternatives})(?!\w)")


def _is_common_word(name: str) -> bool:
    bare = name.lstrip("^")
    return len(bare) == 1 or bare in _COMMON_WORDS


def _reads_as_word(text: str, match: re.Match[str]) -> bool:
    """True when *match* opens a sentence or is followed by a lowercase word."""
    return (
        _opens_sentence(text, match)
        or re.match(r"\s+[a-z]", text[match.end() :]) is not None
    )


def _opens_sentence(text: str, match: re.Match[str]) -> bool:
    before = text[: match.start()].rstrip()
    return not before or before[-1] in ".!?"


def _price_tokens(record: StockRecord) -> frozenset[str]:
    """Return every absolute price level rounded to 0, 1 and 2 dp."""
    a = record.analysis
    levels = [
        record.price,
        record.sma10,
        record.sma30,
        record.sma50,
        record.sma150,
        record.sma200,
        record.high_52w,
        record.low_52w,
        record.high_base,
        record.handle_low,
    ]
    if a is not None:
        levels += [a.entry_price, a.prev_entry_price, a.stop_loss, a.multiyear_pivot]
    return frozenset(
        f"{level:.{dp}f}"
        for level in levels
        if level is not None and math.isfinite(level)
        for dp in (0, 1, 2)
    )


def _leaks_amount(fact: _Fact, tokens: frozenset[str]) -> bool:
    """True when *fact* names a currency amount or (free text) a price level."""
    if CURRENCY_AMOUNT.search(fact.text):
        return True
    if fact.kind not in _FREE_TEXT_KINDS:
        return False
    return _TWO_DP.search(fact.text) is not None or any(
        m.group().replace(",", "") in tokens for m in _NUMBER.finditer(fact.text)
    )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _record_facts(
    record: StockRecord, is_held: bool, as_of: date | None
) -> list[_Fact]:
    """Return the record's facts in a stable order, skipping empty groups."""
    rec = classify_recommendation(record, is_portfolio_holding=is_held)
    held = (
        "is currently held in a portfolio"
        if is_held
        else "is not currently held in any portfolio"
    )
    facts = [
        _Fact(
            "recommendation",
            f"Recommendation: {rec.text or 'none'} — "
            f"{recommendation_reason(record, rec)}.",
            as_of,
            _RULES_SOURCE,
        ),
        _Fact("portfolio", f"{LABEL} {held}.", None, "trade ledger"),
    ]
    a = record.analysis
    groups = [
        ("analysis", _analysis_text(a) if a else None),
        ("setup", _setup_text(record, a) if a else None),
        ("trend", _trend_text(record)),
        ("momentum", _momentum_text(record)),
        ("fundamental", _fundamental_text(record)),
        ("sepa", _sepa_text(a) if a else None),
        ("score", _score_text(a) if a else None),
    ]
    facts += [_Fact(kind, text, as_of) for kind, text in groups if text]
    if a is not None:
        facts += [_Fact("strength", text, as_of) for text in a.strengths]
        facts += [_Fact("risk", text, as_of) for text in a.risks]
        facts.append(_Fact("summary", a.summary, as_of))
    return [fact for fact in facts if fact.text.strip()]


def _join(parts: list[str | None]) -> str | None:
    kept = [part for part in parts if part]
    return "; ".join(kept) + "." if kept else None


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _fmt(template: str, value: float | None) -> str | None:
    """Format *value* into *template*, or None when it is missing or non-finite."""
    finite = _finite(value)
    return None if finite is None else template.format(finite)


def _distance(name: str, level: float | None, price: float) -> str | None:
    """Return *level* as a percent distance from a positive, finite close."""
    finite = _finite(level)
    if finite is None or not math.isfinite(price) or price <= 0:
        return None
    return f"{name} {(finite / price - 1) * 100:+.1f}% from latest close"


def _flag(name: str, value: bool) -> str:
    return f"{name}: {'yes' if value else 'no'}"


def _analysis_text(a: StockAnalysis) -> str | None:
    return _join(
        [
            f"Score {a.score}/10",
            a.stage,
            f"entry zone {a.entry_zone}",
            _flag("volume confirmed", a.volume_confirmed),
            _flag("fresh breakout", a.fresh_breakout),
            _flag("multi-year breakout", a.multiyear_breakout),
        ]
    )


def _setup_text(record: StockRecord, a: StockAnalysis) -> str | None:
    price = record.price
    return _join(
        [
            _distance("Entry", a.entry_price, price),
            _distance("Stop loss", a.stop_loss, price),
            _distance("10-week base high", record.high_base, price),
            _distance("Multi-year pivot", a.multiyear_pivot, price),
            _fmt("risk entry-to-stop {:.1%}", a.risk_pct),
            _fmt("reward/risk {:.1f}R", a.reward_risk_ratio),
        ]
    )


def _trend_text(record: StockRecord) -> str | None:
    price = record.price
    return _join(
        [
            _distance("SMA10", record.sma10, price),
            _distance("SMA30", record.sma30, price),
            _distance("SMA50", record.sma50, price),
            _distance("SMA150", record.sma150, price),
            _distance("SMA200", record.sma200, price),
            _distance("52-week high", record.high_52w, price),
            _distance("52-week low", record.low_52w, price),
        ]
    )


def _momentum_text(record: StockRecord) -> str | None:
    return _join(
        [
            _fmt("RSI(14) {:.0f}", record.rsi14),
            _fmt("relative volume {:.1f}x", record.rel_volume),
            _fmt("week change {:+.1f}%", record.pct_change_week),
            _fmt("{:+.1f}% from 52-week high", record.pct_from_52w_high),
            _fmt("52-week return vs S&P 500 {:+.1f} pts", record.rel_strength_vs_spy),
        ]
    )


def _fundamental_text(record: StockRecord) -> str | None:
    return _join(
        [
            f"sector {record.sector}" if record.sector else None,
            _fmt("quarterly EPS growth {:.0%}", record.eps_growth),
            _fmt("annual EPS growth {:.0%}", record.annual_eps_growth),
            _fmt("ROE {:.0%}", record.roe),
            _fmt("P/E {:.1f}", record.pe_ratio),
            _fmt("institutional ownership {:.0%}", record.inst_ownership_pct),
        ]
    )


def _sepa_text(a: StockAnalysis) -> str | None:
    if not a.sepa_template:
        return None
    passed = [name for name, ok in a.sepa_template.items() if ok]
    failed = [name for name, ok in a.sepa_template.items() if not ok]
    total = len(a.sepa_template)
    return _join(
        [
            f"SEPA trend template {len(passed)}/{total} passed",
            f"failed: {', '.join(failed)}" if failed else None,
        ]
    )


def _score_text(a: StockAnalysis) -> str | None:
    return _join(
        [
            f"CANSLIM {a.canslim.total}/14" if a.canslim else None,
            f"technical momentum {a.momentum.total}/14" if a.momentum else None,
        ]
    )


def _limitations(
    record: StockRecord,
    meta: AnalysisArtifactMeta | None,
    freshness: Freshness,
    source_health: Mapping[SourceName, SourceHealth],
) -> list[_Fact]:
    """Return every gap the answer must admit, as limitation facts."""
    limits: list[_Fact] = []
    if record.analysis is None:
        limits.append(_limit(f"No analysis section is recorded for {LABEL}."))
    if meta is None:
        limits.append(
            _limit("The analysis run identity is unknown (legacy or unreadable file).")
        )
    refreshed = freshness.refreshed_at.date() if freshness.refreshed_at else None
    if freshness.state is FreshnessState.STALE:
        limits.append(
            _limit(
                f"The analysis is stale: generated {freshness.age_display}, "
                "past its freshness window.",
                refreshed,
                "freshness check",
            )
        )
    elif freshness.state is FreshnessState.UNKNOWN:
        limits.append(
            _limit("The analysis age is unknown, so freshness is unconfirmed.")
        )
    if freshness.diagnostic:
        limits.append(_limit(freshness.diagnostic, source="freshness check"))
    for name in sorted(source_health):
        limits += _source_limits(source_health[name])
    return limits


def _source_limits(health: SourceHealth) -> list[_Fact]:
    limits: list[_Fact] = []
    gap = _SOURCE_GAPS.get(health.state)
    if gap is not None:
        detail = f" ({health.detail_code})" if health.detail_code else ""
        limits.append(
            _limit(
                f"The {health.label} source {gap} in this run{detail}.",
                source=health.label,
            )
        )
    if health.data_as_of is not None:
        limits.append(
            _limit(
                f"The {health.label} source reused cached input from "
                f"{health.data_as_of.isoformat()}.",
                health.data_as_of,
                health.label,
            )
        )
    return limits


def _limit(
    text: str, as_of: date | None = None, source: str = _ANALYSIS_SOURCE
) -> _Fact:
    return _Fact(LIMITATION_KIND, text, as_of, source)
