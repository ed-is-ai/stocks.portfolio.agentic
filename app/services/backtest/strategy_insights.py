"""Explicit allowlist projection, validation, caching, and audit for model calls."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import math
import re
from typing import Literal

from app.agents.strategy_insights.agent import (
    StrategyAgentGenerationV1,
    StrategyManagerInsightsAgent,
)
from app.repositories.backtest_repo import BacktestRepository
from app.schemas.strategy_insights import (
    StrategyAgentAttemptV1,
    StrategyInsightClaimV1,
    StrategyInsightExclusionV1,
    StrategyInsightMetricsV1,
    StrategyInsightParameterV1,
    StrategyInsightProvenanceV1,
    StrategyInsightRunV1,
    StrategyInsightSummaryV1,
    StrategyInsightsReportV1,
    StrategyInsightsRequestV1,
    StrategyQuestionAnswerV1,
    StrategyQuestionContextV1,
    StrategyQuestionRequestV1,
)
from app.schemas.strategy_outcomes import OutcomeRunV1, StrategyOutcomeSummaryV1

logger = logging.getLogger(__name__)
_MAX_TOTAL_TICKERS = 5_000
_INSIGHT_PROMPT_VERSION = "strategy-manager-insights.prompt.v2"
_INSIGHT_SCHEMA_VERSION = "strategy-manager-insights.schema.v2"
_QUESTION_PROMPT_VERSION = "strategy-manager-copilot.prompt.v2"
_QUESTION_SCHEMA_VERSION = "strategy-manager-copilot.schema.v2"
_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d*)?|\.\d+)%?(?![A-Za-z])")
_NUMBER_WORD_ATOM = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)"
_NUMBER_WORD_PHRASE = re.compile(
    rf"\b{_NUMBER_WORD_ATOM}(?:[\s-]+(?:and[\s-]+)?{_NUMBER_WORD_ATOM})*\b",
    re.IGNORECASE,
)
_TICKER_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,31}$")
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
MetricName = Literal["total_return", "sharpe_ratio", "win_rate", "max_drawdown"]
_SENSITIVE_PARAMETER_PARTS = frozenset(
    {
        "account",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "key",
        "password",
        "passwd",
        "portfolio",
        "secret",
        "token",
    }
)


def is_shareable_strategy_parameter(name: str) -> bool:
    """Exclude selector, account, and credential-like names from provider payloads."""
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    lowered = normalized.casefold()
    parts = set(re.split(r"[^A-Za-z0-9]+", lowered))
    if parts & _SENSITIVE_PARAMETER_PARTS:
        return False
    if any(token in lowered for token in ("universe", "ticker", "security_id")):
        return False
    return not any(
        token in normalized.casefold()
        for token in ("password", "passwd", "credential", "authorization", "secret")
    )


@dataclass(frozen=True)
class StrategyManagerAgentOutcomeV1:
    state: Literal["completed", "unavailable"]
    request_digest: str
    summary_digest: str
    report: StrategyInsightsReportV1 | None = None
    answer: StrategyQuestionAnswerV1 | None = None
    attempts: tuple[StrategyAgentAttemptV1, ...] = ()
    cached: bool = False
    message: str | None = None


class StrategyInsightValidationError(ValueError):
    """A structurally valid response makes a claim outside the supplied evidence."""


@dataclass(frozen=True)
class _AllowedNumbersV1:
    plain: set[Decimal]
    percent: set[Decimal]


def project_strategy_summary(
    summary: StrategyOutcomeSummaryV1,
) -> StrategyInsightSummaryV1:
    """Construct the provider summary field-by-field; never serialize a Result row."""
    runs: list[StrategyInsightRunV1] = []
    tickers_remaining = _MAX_TOTAL_TICKERS
    for row in summary.runs[:25]:
        parameters = _scalar_parameters(row)
        source_tickers = tuple(row.universe)
        ticker_symbols = tuple(
            symbol
            for label in source_tickers
            if (symbol := label.partition(" (")[0].strip())
            and symbol.lower() != "unknown security"
            and _TICKER_SYMBOL.fullmatch(symbol)
        )
        tickers = ticker_symbols[: min(1000, tickers_remaining)]
        tickers_remaining -= len(tickers)
        availability: dict[MetricName, str | None] = {
            name: row.metric_availability.get(name)
            or ("unavailable" if getattr(row.metrics, name) is None else None)
            for name in ("total_return", "sharpe_ratio", "win_rate", "max_drawdown")
        }
        runs.append(
            StrategyInsightRunV1(
                evidence_handle=row.evidence_handle,
                strategy_id=row.strategy_id,
                strategy_api_version=row.strategy_api_version,
                start_month=row.start_month,
                end_month=row.end_month,
                currency=row.base_currency,
                parameters=parameters,
                universe_tickers=tickers,
                universe_symbol_count=len(ticker_symbols),
                universe_symbols_truncated=len(tickers) < len(ticker_symbols),
                metrics=StrategyInsightMetricsV1(
                    total_return=row.metrics.total_return,
                    sharpe_ratio=row.metrics.sharpe_ratio,
                    win_rate=row.metrics.win_rate,
                    max_drawdown=row.metrics.max_drawdown,
                ),
                metric_availability=availability,
                closed_trade_count=row.closed_trade_count,
                candidate_count=row.candidate_count,
                provenance=tuple(
                    StrategyInsightProvenanceV1(
                        quality=item.quality, snapshot_count=item.snapshot_count
                    )
                    for item in row.provenance[:12]
                ),
                is_pinned_spy_reference=row.is_spy_reference,
            )
        )

    job_exclusions = _exclusions(summary.job_exclusions)
    comparison_exclusions = _exclusions(summary.comparison_exclusions)
    content = {
        "state": summary.state,
        "period_start": summary.cohort.period_start,
        "period_end": summary.cohort.period_end,
        "currency": summary.cohort.base_currency,
        "inspected_count": summary.inspected_count,
        "verified_count": summary.verified_count,
        "cohort_candidate_count": summary.cohort_candidate_count,
        "cohort_strategy_count": summary.cohort_strategy_count,
        "integrity_excluded_count": summary.integrity_excluded_count,
        "missing_result_count": summary.missing_result_count,
        "job_exclusions": job_exclusions,
        "comparison_exclusions": comparison_exclusions,
        "limitations": tuple(summary.cohort.limitations[:12]),
        "runs": tuple(runs),
    }
    digest = _digest(content)
    return StrategyInsightSummaryV1(summary_digest=digest, **content)


def _scalar_parameters(row: OutcomeRunV1) -> tuple[StrategyInsightParameterV1, ...]:
    params: list[StrategyInsightParameterV1] = []
    for name, value in sorted(row.parameters.items()):
        lowered_name = name.lower()
        if any(
            token in lowered_name for token in ("universe", "ticker", "security_id")
        ) or not is_shareable_strategy_parameter(name):
            continue
        if value is not None and type(value) not in (str, int, float, bool):
            continue
        if isinstance(value, str) and len(value) > 256:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        try:
            params.append(StrategyInsightParameterV1(name=name, value=value))
        except ValueError:
            continue
    return tuple(params[:64])


def _exclusions(values: tuple[object, ...]) -> tuple[StrategyInsightExclusionV1, ...]:
    result: list[StrategyInsightExclusionV1] = []
    for value in values[:25]:
        reason = str(getattr(value, "reason", "other"))
        if not _SAFE_REASON.fullmatch(reason):
            reason = "other"
        result.append(
            StrategyInsightExclusionV1(
                reason=reason,
                count=max(0, int(getattr(value, "count", 0))),
            )
        )
    return tuple(result)


def _json_value(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _digest(payload: object) -> str:
    encoded = json.dumps(
        _json_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_insights_report(
    report: StrategyInsightsReportV1, summary: StrategyInsightSummaryV1
) -> None:
    rows_by_handle = {row.evidence_handle: row for row in summary.runs}
    handles = set(rows_by_handle)
    claims: tuple[StrategyInsightClaimV1, ...] = (
        *report.observations,
        *report.hypotheses,
    )
    for claim in claims:
        _validate_citations(claim.evidence_handles, handles)
        cited_rows = tuple(rows_by_handle[handle] for handle in claim.evidence_handles)
        cited_strategy_ids = {row.strategy_id for row in cited_rows}
        if not set(claim.strategy_ids) <= cited_strategy_ids:
            raise StrategyInsightValidationError(
                "response named a Strategy outside its cited evidence"
            )
        allowed_numbers = _allowed_numeric_tokens(summary, claim.evidence_handles)
        _validate_numeric_text(claim.text, allowed_numbers)
        _validate_strategy_identity_mentions(claim.text, summary, cited_strategy_ids)
        if _has_future_or_causal_claim(claim.text):
            raise StrategyInsightValidationError(
                "response made a causal or future-return claim"
            )
    for idea in report.strategies_to_explore:
        _validate_citations(idea.evidence_handles, handles)
        cited_rows = tuple(rows_by_handle[handle] for handle in idea.evidence_handles)
        cited_strategy_ids = {row.strategy_id for row in cited_rows}
        text = idea.title + " " + idea.description
        _validate_numeric_text(
            text, _allowed_numeric_tokens(summary, idea.evidence_handles)
        )
        _validate_strategy_identity_mentions(text, summary, cited_strategy_ids)
        if _has_future_or_causal_claim(text):
            raise StrategyInsightValidationError(
                "strategy idea made a future-return or causal claim"
            )


def validate_question_answer(
    answer: StrategyQuestionAnswerV1,
    request: StrategyQuestionRequestV1,
) -> StrategyQuestionAnswerV1:
    rows_by_handle = {row.evidence_handle: row for row in request.summary.runs}
    _validate_citations(answer.citations, set(rows_by_handle))
    cited_rows = tuple(rows_by_handle[handle] for handle in answer.citations)
    cited_strategy_ids = {row.strategy_id for row in cited_rows}
    current_strategy_id = request.current_strategy.strategy_id
    allowed_numbers = _allowed_numeric_tokens(
        request.summary,
        answer.citations,
        request.current_strategy.declared_parameters,
    )
    _validate_numeric_text(answer.answer, allowed_numbers)
    _validate_strategy_identity_mentions(
        answer.answer,
        request.summary,
        cited_strategy_ids | {current_strategy_id},
    )
    if _has_future_or_causal_claim(answer.answer):
        raise StrategyInsightValidationError(
            "response made a causal or future-return claim"
        )
    for unknown in answer.unknowns:
        _validate_numeric_text(unknown, allowed_numbers)
        _validate_strategy_identity_mentions(
            unknown,
            request.summary,
            cited_strategy_ids | {current_strategy_id},
        )
        if _has_future_or_causal_claim(unknown):
            raise StrategyInsightValidationError(
                "response included an unsupported claim in unknowns"
            )
    # Preserve all deterministic limitations before any provider-supplied text.
    unknowns = tuple(dict.fromkeys((*request.summary.limitations, *answer.unknowns)))[
        :12
    ]
    return StrategyQuestionAnswerV1.model_validate(
        {**answer.model_dump(mode="python"), "unknowns": unknowns}
    )


def _validate_citations(citations: tuple[str, ...], allowed: set[str]) -> None:
    if not citations or any(handle not in allowed for handle in citations):
        raise StrategyInsightValidationError(
            "response cited an unavailable evidence handle"
        )


def _allowed_numeric_tokens(
    summary: StrategyInsightSummaryV1,
    evidence_handles: tuple[str, ...] | None = None,
    current_parameters: tuple[StrategyInsightParameterV1, ...] = (),
) -> _AllowedNumbersV1:
    """Allow only numeric fields in cited rows plus shared cohort metadata."""
    allowed = _AllowedNumbersV1(plain=set(), percent=set())

    for value in (
        summary.inspected_count,
        summary.verified_count,
        summary.cohort_candidate_count,
        summary.cohort_strategy_count,
        summary.integrity_excluded_count,
        summary.missing_result_count,
    ):
        _add_numeric_value(allowed, value)
    for item in (*summary.job_exclusions, *summary.comparison_exclusions):
        _add_numeric_value(allowed, item.count)
    for month in (summary.period_start, summary.period_end):
        _add_numeric_text_values(allowed, month or "")

    selected = set(evidence_handles) if evidence_handles is not None else None
    for row in summary.runs:
        if selected is not None and row.evidence_handle not in selected:
            continue
        _add_numeric_value(allowed, row.strategy_api_version)
        _add_numeric_text_values(allowed, row.start_month)
        _add_numeric_text_values(allowed, row.end_month)
        for name, value in row.metrics.model_dump(mode="python").items():
            _add_numeric_value(
                allowed,
                value,
                percentage=name in {"total_return", "win_rate", "max_drawdown"},
            )
        for parameter in row.parameters:
            _add_numeric_value(allowed, parameter.value)
        _add_numeric_value(allowed, row.universe_symbol_count)
        _add_numeric_value(allowed, row.closed_trade_count)
        _add_numeric_value(allowed, row.candidate_count)
        for provenance in row.provenance:
            _add_numeric_value(allowed, provenance.snapshot_count)
    for parameter in current_parameters:
        _add_numeric_value(allowed, parameter.value)
    return allowed


def _add_numeric_value(
    allowed: _AllowedNumbersV1, value: object, *, percentage: bool = False
) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return
        allowed.plain.add(number.normalize())
        if percentage:
            allowed.percent.add((number * 100).normalize())
        return
    if isinstance(value, str):
        _add_numeric_text_values(allowed, value)


def _add_numeric_text_values(allowed: _AllowedNumbersV1, text: str) -> None:
    for token in _NUMBER_TOKEN.findall(text):
        try:
            number = Decimal(token.rstrip("%")).normalize()
        except InvalidOperation:
            continue
        (allowed.percent if token.endswith("%") else allowed.plain).add(number)


def _validate_numeric_text(text: str, allowed: _AllowedNumbersV1) -> None:
    for token in _NUMBER_TOKEN.findall(text):
        try:
            number = Decimal(token.rstrip("%")).normalize()
        except InvalidOperation as exc:
            raise StrategyInsightValidationError(
                "response contained an invalid number"
            ) from exc
        permitted = allowed.percent if token.endswith("%") else allowed.plain
        if number not in permitted:
            raise StrategyInsightValidationError(
                "response introduced a value absent from the cited evidence"
            )
    for match in _NUMBER_WORD_PHRASE.finditer(text.lower()):
        number = _parse_number_words(match.group())
        if number is None:
            continue
        following = text[match.end() :].lstrip().casefold()
        words = re.findall(r"[a-z]+", match.group())
        count_context = re.match(
            r"(?:percent|percentage|strategy|strategies|result|results|run|runs|"
            r"trade|trades|candidate|candidates|ticker|tickers|point|points|"
            r"month|months|year|years|day|days|week|weeks|snapshot|snapshots|"
            r"symbol|symbols|position|positions|holding|holdings)\b",
            following,
        )
        if len(words) == 1 and not count_context and not following.startswith("%"):
            continue
        is_percent = following.startswith(("percent", "percentage", "%"))
        permitted = allowed.percent if is_percent else allowed.plain
        if number.normalize() not in permitted:
            raise StrategyInsightValidationError(
                "response introduced a number absent from the cited evidence"
            )


def _parse_number_words(phrase: str) -> Decimal | None:
    small = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
    }
    tens = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }
    total = current = 0
    seen = False
    for word in re.findall(r"[a-z]+", phrase):
        if word == "and":
            continue
        if word in small:
            current += small[word]
            seen = True
        elif word in tens:
            current += tens[word]
            seen = True
        elif word == "hundred":
            current = max(current, 1) * 100
            seen = True
        elif word in {"thousand", "million"}:
            scale = 1_000 if word == "thousand" else 1_000_000
            total += max(current, 1) * scale
            current = 0
            seen = True
    return Decimal(total + current) if seen else None


def _validate_strategy_identity_mentions(
    text: str,
    summary: StrategyInsightSummaryV1,
    allowed_strategy_ids: set[str],
) -> None:
    lowered = text.casefold()
    all_ids = {row.strategy_id for row in summary.runs}
    for strategy_id in all_ids:
        if (
            strategy_id.casefold() in lowered
            and strategy_id not in allowed_strategy_ids
        ):
            raise StrategyInsightValidationError(
                "response named a Strategy outside its cited evidence"
            )
        aliases = {strategy_id.casefold()}
        prefix = "rtly-backtest-"
        if strategy_id.casefold().startswith(prefix):
            suffix = strategy_id[len(prefix) :]
            aliases.add(suffix.casefold())
            aliases.add(suffix.replace("-", " ").casefold())
        if strategy_id not in allowed_strategy_ids and any(
            alias
            and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered)
            for alias in aliases
        ):
            raise StrategyInsightValidationError(
                "response named a Strategy outside its cited evidence"
            )
    for token in re.findall(r"\brtly-backtest-[A-Za-z0-9][A-Za-z0-9._-]*\b", text):
        if token not in allowed_strategy_ids:
            raise StrategyInsightValidationError(
                "response introduced an unsupported Strategy identity"
            )


def _has_future_or_causal_claim(text: str) -> bool:
    lowered = text.lower()
    future_patterns = (
        r"\b(?:will|would|should|could|may|might)\s+(?:outperform|return|profit|gain|improve|increase|decrease|reduce|perform|benefit|succeed)\b",
        r"\b(?:guarantee(?:s|d)?|future returns?)\b",
    )
    causal_pattern = (
        r"\b(?:caus(?:e|es|ed|ation)|because|due to|owing to|as a result of|"
        r"leads? to|results? in|driven by|explains?|accounts for|"
        r"(?:improves?|reduces?|increases?|decreases?|boosts?|raises?|lowers?)"
        r"\s+(?:(?:the|a|an)\s+)?(?:returns?|performance|win\s+rate|"
        r"drawdown|profits?|risk|success|results?))\b"
    )
    for pattern in future_patterns:
        matches = re.finditer(pattern, lowered)
        for match in matches:
            if not _negated_claim_before(lowered, match.start()):
                return True
    for match in re.finditer(causal_pattern, lowered):
        if not _negated_claim_before(lowered, match.start()):
            return True
    return False


def _negated_claim_before(text: str, start: int) -> bool:
    clause_start = max(text.rfind(mark, 0, start) for mark in ".!?;") + 1
    prefix = text[clause_start:start]
    return (
        re.search(
            r"\b(?:cannot|can't|won't|wouldn't|shouldn't|not|no|never|doesn't|didn't|do not|does not)\b[^.!?]{0,80}$",
            prefix,
        )
        is not None
    )


class StrategyManagerInsightsService:
    """Local payload construction, digest cache, and append-only call audit."""

    def __init__(
        self, repository: BacktestRepository, agent: StrategyManagerInsightsAgent
    ) -> None:
        self._repository = repository
        self._agent = agent

    def generate_insights(
        self,
        summary: StrategyOutcomeSummaryV1,
        *,
        refresh: bool = False,
    ) -> StrategyManagerAgentOutcomeV1:
        request = StrategyInsightsRequestV1(summary=project_strategy_summary(summary))
        request_digest = _digest(
            {
                "prompt_version": _INSIGHT_PROMPT_VERSION,
                "schema_version": _INSIGHT_SCHEMA_VERSION,
                "summary": request.summary.model_dump(mode="json"),
            }
        )
        cached = self._repository.strategy_manager_agent_cache(
            "insights", request_digest
        )
        last_good: StrategyInsightsReportV1 | None = None
        if cached is not None:
            try:
                last_good = StrategyInsightsReportV1.model_validate_json(cached)
                validate_insights_report(last_good, request.summary)
                if not refresh:
                    return StrategyManagerAgentOutcomeV1(
                        "completed",
                        request_digest,
                        request.summary.summary_digest,
                        report=last_good,
                        cached=True,
                    )
            except Exception:
                logger.warning(
                    "Cached Strategy insights failed local validation", exc_info=True
                )
                last_good = None

        if not request.summary.runs:
            return StrategyManagerAgentOutcomeV1(
                "unavailable",
                request_digest,
                request.summary.summary_digest,
                message="Verified Strategy outcome rows are not available to summarize.",
            )

        generation = self._agent.generate_insights(
            request,
            validate=lambda report: validate_insights_report(report, request.summary),
        )
        report = (
            generation.output
            if isinstance(generation.output, StrategyInsightsReportV1)
            else None
        )
        if report is not None:
            try:
                validate_insights_report(report, request.summary)
            except StrategyInsightValidationError:
                logger.warning(
                    "Provider report failed service validation", exc_info=True
                )
                report = None
        self._record(
            "insights",
            request_digest,
            request.summary.summary_digest,
            generation,
            report.model_dump(mode="json") if report is not None else None,
            _report_citations(report) if report is not None else (),
            prompt_version=_INSIGHT_PROMPT_VERSION,
            schema_version=_INSIGHT_SCHEMA_VERSION,
        )
        if report is None and last_good is not None:
            return StrategyManagerAgentOutcomeV1(
                "completed",
                request_digest,
                request.summary.summary_digest,
                report=last_good,
                attempts=generation.attempts,
                cached=True,
                message="Refresh failed; showing the last validated report.",
            )
        return StrategyManagerAgentOutcomeV1(
            "completed" if report is not None else "unavailable",
            request_digest,
            request.summary.summary_digest,
            report=report,
            attempts=generation.attempts,
            message=None
            if report is not None
            else "No provider returned a validated report.",
        )

    def record_unavailable_question(
        self,
        summary: StrategyOutcomeSummaryV1,
        *,
        question: str,
        reason: str,
    ) -> None:
        """Audit a submitted question that cannot be sent to a provider."""
        projected = project_strategy_summary(summary)
        request_digest = _digest(
            {
                "prompt_version": _QUESTION_PROMPT_VERSION,
                "schema_version": _QUESTION_SCHEMA_VERSION,
                "summary": projected.model_dump(mode="json"),
                "question": question,
                "not_sent_reason": reason,
            }
        )
        self._repository.record_strategy_manager_agent_call(
            task="question",
            request_digest=request_digest,
            summary_digest=projected.summary_digest,
            user_question=question,
            attempts=(),
            prompt_version=_QUESTION_PROMPT_VERSION,
            schema_version=_QUESTION_SCHEMA_VERSION,
            outcome="unavailable",
            accepted_citations=(),
            output=None,
        )

    def ask(
        self,
        summary: StrategyOutcomeSummaryV1,
        *,
        question: str,
        strategy: StrategyQuestionContextV1,
    ) -> StrategyManagerAgentOutcomeV1:
        request = StrategyQuestionRequestV1(
            summary=project_strategy_summary(summary),
            question=question,
            current_strategy=strategy,
        )
        request_digest = _digest(
            {
                "prompt_version": _QUESTION_PROMPT_VERSION,
                "schema_version": _QUESTION_SCHEMA_VERSION,
                "request": request.model_dump(mode="json"),
            }
        )
        if not request.summary.runs:
            self._repository.record_strategy_manager_agent_call(
                task="question",
                request_digest=request_digest,
                summary_digest=request.summary.summary_digest,
                user_question=request.question,
                attempts=(),
                prompt_version=_QUESTION_PROMPT_VERSION,
                schema_version=_QUESTION_SCHEMA_VERSION,
                outcome="unavailable",
                accepted_citations=(),
                output=None,
            )
            return StrategyManagerAgentOutcomeV1(
                "unavailable",
                request_digest,
                request.summary.summary_digest,
                message="Verified Strategy outcome rows are not available for this question.",
            )

        def validate_answer(answer: StrategyQuestionAnswerV1) -> None:
            validate_question_answer(answer, request)

        generation = self._agent.answer_question(
            request,
            validate=validate_answer,
        )
        answer = (
            generation.output
            if isinstance(generation.output, StrategyQuestionAnswerV1)
            else None
        )
        if answer is not None:
            try:
                answer = validate_question_answer(answer, request)
            except StrategyInsightValidationError:
                logger.warning(
                    "Provider answer failed service validation", exc_info=True
                )
                answer = None
        self._record(
            "question",
            request_digest,
            request.summary.summary_digest,
            generation,
            answer.model_dump(mode="json") if answer is not None else None,
            answer.citations if answer is not None else (),
            user_question=request.question,
            prompt_version=_QUESTION_PROMPT_VERSION,
            schema_version=_QUESTION_SCHEMA_VERSION,
        )
        return StrategyManagerAgentOutcomeV1(
            "completed" if answer is not None else "unavailable",
            request_digest,
            request.summary.summary_digest,
            answer=answer,
            attempts=generation.attempts,
            message=None
            if answer is not None
            else "No provider returned a validated answer.",
        )

    def _record(
        self,
        task: Literal["insights", "question"],
        request_digest: str,
        summary_digest: str,
        generation: StrategyAgentGenerationV1,
        output: dict[str, object] | None,
        citations: tuple[str, ...],
        *,
        user_question: str | None = None,
        prompt_version: str,
        schema_version: str,
    ) -> None:
        self._repository.record_strategy_manager_agent_call(
            task=task,
            request_digest=request_digest,
            summary_digest=summary_digest,
            prompt_version=prompt_version,
            schema_version=schema_version,
            attempts=tuple(
                item.model_dump(mode="json") for item in generation.attempts
            ),
            outcome="accepted" if output is not None else "unavailable",
            accepted_citations=citations,
            output=output,
            user_question=user_question,
        )


def _report_citations(report: StrategyInsightsReportV1) -> tuple[str, ...]:
    handles = [
        *(
            handle
            for claim in (*report.observations, *report.hypotheses)
            for handle in claim.evidence_handles
        ),
        *(
            handle
            for idea in report.strategies_to_explore
            for handle in idea.evidence_handles
        ),
    ]
    return tuple(dict.fromkeys(handles))


__all__ = [
    "StrategyInsightValidationError",
    "StrategyManagerAgentOutcomeV1",
    "StrategyManagerInsightsService",
    "project_strategy_summary",
    "validate_insights_report",
    "validate_question_answer",
]
