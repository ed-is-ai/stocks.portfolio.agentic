"""Which point-in-time roster members a snapshot month holds (#82).

Dependency-free so the engine and the repository share one rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Protocol, TypeVar

POINT_IN_TIME_POLICY_VERSION = "PointInTimeRosterPolicyV2"

#: ``((start, end | None), ...)`` ISO dates; each interval is ``[start, end)``.
MembershipIntervals = tuple[tuple[str, str | None], ...]


class IntervalMember(Protocol):
    """A roster member with an exchange and index membership intervals."""

    @property
    def security_id(self) -> str: ...

    @property
    def mic(self) -> str: ...

    @property
    def membership_intervals(self) -> MembershipIntervals: ...


M = TypeVar("M", bound=IntervalMember)


def is_member_on(intervals: MembershipIntervals, session: date) -> bool:
    """Whether one ``[start, end)`` interval contains ``session``."""
    return any(
        date.fromisoformat(start) <= session
        and (end is None or session < date.fromisoformat(end))
        for start, end in intervals
    )


def month_members(
    members: Iterable[M],
    sessions: Mapping[str, date],
    *,
    point_in_time: bool,
) -> list[M]:
    """Return a month's members, ordered by security id.

    A V1 roster contributes every member to every month. A point-in-time
    roster contributes only the members one of whose membership intervals
    contains the month's as-of session on their MIC (``sessions``); members
    without intervals are never in a reconstructed month.
    """
    ordered = sorted(members, key=lambda item: item.security_id)
    if not point_in_time:
        return ordered
    return [
        member
        for member in ordered
        if is_member_on(member.membership_intervals, _session(sessions, member.mic))
    ]


def _session(sessions: Mapping[str, date], mic: str) -> date:
    try:
        return sessions[mic]
    except KeyError as exc:
        raise ValueError(f"no month as-of session for MIC {mic!r}") from exc
