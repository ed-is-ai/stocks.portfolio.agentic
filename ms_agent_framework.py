from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Agent(BaseModel):
    name: str

    def run(self, payload: Any) -> Any:
        raise NotImplementedError("Agent.run must be implemented by subclasses")

    def describe(self) -> str:
        return f"Agent: {self.name}"


class AgentApp(BaseModel):
    name: str
    agents: list[Agent] = Field(default_factory=list)

    def add_agent(self, agent: Agent) -> None:
        self.agents.append(agent)

    def execute(self, payload: Any) -> Any:
        current = payload
        for agent in self.agents:
            current = agent.run(current)
        return current

    def execute_with_intermediates(self, payload: Any) -> tuple[Any, list[Any]]:
        """Execute pipeline and return (final_result, intermediate_results)"""
        current = payload
        intermediates = []
        for agent in self.agents:
            current = agent.run(current)
            intermediates.append(current)
        return current, intermediates[:-1]  # Exclude final result from intermediates
