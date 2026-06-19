"""Base class for pipeline-stage agents.

A minimal Pydantic base giving every agent a ``name`` and a ``run`` contract.
Agents stay pure (``run(payload) -> result``) and ignorant of the pipeline; the
typed stage contract lives in the Step adapters under ``app/workflows``.

(Relocated from the former ``ms_agent_framework`` module, whose untyped
``AgentApp`` is replaced by ``app.workflows.pipeline.Pipeline``.)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Agent(BaseModel):
    """Common base for agents — a named unit with a ``run`` method."""

    name: str

    def run(self, payload: Any) -> Any:
        raise NotImplementedError("Agent.run must be implemented by subclasses")

    def describe(self) -> str:
        return f"Agent: {self.name}"
