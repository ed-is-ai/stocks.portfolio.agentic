"""Statically-typed linear pipeline.

Replaces the former untyped ``ms_agent_framework.AgentApp``. Stages are composed
through a ``.then()`` builder that advances the output type while pinning the
input type, so a mis-ordered stage is a type error at the wiring site rather than
a runtime surprise. Validation is static-only: payloads are passed between stages
without re-parsing or re-validation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, Protocol, Sized, TypeVar

logger = logging.getLogger(__name__)

TIn = TypeVar("TIn")
TOut = TypeVar("TOut")
TNext = TypeVar("TNext")


@dataclass(frozen=True)
class PipelineStepEvent:
    """A typed lifecycle notification emitted around a pipeline step."""

    step_name: str
    phase: Literal["started", "completed"]
    output_count: int | None = None


ProgressCallback = Callable[[PipelineStepEvent], None]


class Step(Protocol[TIn, TOut]):
    """A single pipeline stage: a named ``run(payload) -> result``."""

    name: str

    def run(self, payload: TIn) -> TOut: ...


class Pipeline(Generic[TIn, TOut]):
    """A linear chain of Steps with a compile-time stage contract.

    Build with ``Pipeline.start(step).then(step)...``; each ``.then`` requires
    the next Step's input type to equal the current output type. ``run`` returns
    the final Step's output; ``run_traced`` additionally returns every stage's
    ``(name, output)`` and logs per-stage timing.
    """

    def __init__(self, steps: list[Step[Any, Any]]) -> None:
        self._steps = steps

    @classmethod
    def start(cls, step: Step[TIn, TOut]) -> Pipeline[TIn, TOut]:
        """Begin a pipeline with its first Step."""
        return cls([step])

    def then(self, step: Step[TOut, TNext]) -> Pipeline[TIn, TNext]:
        """Append a Step whose input type is the current output type."""
        return Pipeline([*self._steps, step])

    def run(self, payload: TIn) -> TOut:
        """Run the pipeline and return the final Step's output."""
        result, _ = self.run_traced(payload)
        return result

    def run_traced(
        self, payload: TIn, *, progress: ProgressCallback | None = None
    ) -> tuple[TOut, list[tuple[str, object]]]:
        """Run the pipeline, returning the final output and a per-stage trace.

        The trace is an ordered list of ``(stage_name, stage_output)`` for every
        stage, suitable for feeding intermediates (e.g. to the Excel export).
        """
        current: Any = payload
        trace: list[tuple[str, object]] = []
        for step in self._steps:
            if progress:
                progress(PipelineStepEvent(step_name=step.name, phase="started"))
            started = time.perf_counter()
            current = step.run(current)
            elapsed = time.perf_counter() - started
            logger.info("pipeline step '%s' completed in %.2fs", step.name, elapsed)
            trace.append((step.name, current))
            if progress:
                count = len(current) if isinstance(current, Sized) else 1
                progress(
                    PipelineStepEvent(
                        step_name=step.name,
                        phase="completed",
                        output_count=count,
                    )
                )
        return current, trace
