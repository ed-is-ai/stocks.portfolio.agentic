"""
Unit tests for MS Agent Framework.
"""

import pytest
from ms_agent_framework import Agent, AgentApp


class MockAgent(Agent):
    """Mock agent for testing."""

    name: str
    multiplier: int = 1
    call_count: int = 0

    def run(self, payload):
        self.call_count += 1
        if isinstance(payload, (int, float)):
            return payload * self.multiplier
        elif isinstance(payload, list):
            return [item * self.multiplier for item in payload]
        return payload


class TestAgent:
    """Test base Agent class."""

    def test_agent_creation(self):
        """Test agent creation."""
        agent = MockAgent(name="TestAgent")
        assert agent.name == "TestAgent"
        assert agent.call_count == 0

    def test_agent_describe(self):
        """Test agent description."""
        agent = MockAgent(name="TestAgent")
        assert agent.describe() == "Agent: TestAgent"


class TestAgentApp:
    """Test AgentApp functionality."""

    def test_app_creation(self):
        """Test AgentApp creation."""
        app = AgentApp(name="TestApp")
        assert app.name == "TestApp"
        assert len(app.agents) == 0

    def test_add_agent(self):
        """Test adding agents to app."""
        app = AgentApp(name="TestApp")
        agent1 = MockAgent(name="Agent1")
        agent2 = MockAgent(name="Agent2")

        app.add_agent(agent1)
        app.add_agent(agent2)

        assert len(app.agents) == 2
        assert app.agents[0] == agent1
        assert app.agents[1] == agent2

    def test_execute_single_agent(self):
        """Test executing with single agent."""
        app = AgentApp(name="TestApp")
        agent = MockAgent(name="Multiplier", multiplier=2)
        app.add_agent(agent)

        result = app.execute(5)

        assert result == 10
        assert agent.call_count == 1

    def test_execute_multiple_agents(self):
        """Test executing with multiple agents in chain."""
        app = AgentApp(name="TestApp")
        agent1 = MockAgent(name="Double", multiplier=2)
        agent2 = MockAgent(name="Triple", multiplier=3)
        agent3 = MockAgent(name="AddOne", multiplier=1)  # No change

        app.add_agent(agent1)
        app.add_agent(agent2)
        app.add_agent(agent3)

        result = app.execute(5)

        # 5 -> 10 -> 30 -> 30
        assert result == 30
        assert agent1.call_count == 1
        assert agent2.call_count == 1
        assert agent3.call_count == 1

    def test_execute_with_list_payload(self):
        """Test executing with list payload."""
        app = AgentApp(name="TestApp")
        agent = MockAgent(name="DoubleList", multiplier=2)
        app.add_agent(agent)

        result = app.execute([1, 2, 3])

        assert result == [2, 4, 6]
        assert agent.call_count == 1

    def test_execute_with_intermediates_single_agent(self):
        """Test execute_with_intermediates with single agent."""
        app = AgentApp(name="TestApp")
        agent = MockAgent(name="Double", multiplier=2)
        app.add_agent(agent)

        final_result, intermediates = app.execute_with_intermediates(5)

        assert final_result == 10
        assert len(intermediates) == 0  # No intermediates for single agent

    def test_execute_with_intermediates_multiple_agents(self):
        """Test execute_with_intermediates with multiple agents."""
        app = AgentApp(name="TestApp")
        agent1 = MockAgent(name="Double", multiplier=2)
        agent2 = MockAgent(name="Triple", multiplier=3)
        agent3 = MockAgent(name="Quadruple", multiplier=4)

        app.add_agent(agent1)
        app.add_agent(agent2)
        app.add_agent(agent3)

        final_result, intermediates = app.execute_with_intermediates(5)

        # 5 -> 10 -> 30 -> 120
        assert final_result == 120
        assert len(intermediates) == 2  # Two intermediate results
        assert intermediates[0] == 10   # After agent1
        assert intermediates[1] == 30   # After agent2

    def test_execute_with_intermediates_empty_app(self):
        """Test execute_with_intermediates with no agents."""
        app = AgentApp(name="EmptyApp")

        final_result, intermediates = app.execute_with_intermediates(42)

        assert final_result == 42
        assert len(intermediates) == 0

    def test_execute_empty_app(self):
        """Test execute with no agents."""
        app = AgentApp(name="EmptyApp")

        result = app.execute(42)

        assert result == 42

    def test_agent_call_order(self):
        """Test that agents are called in the correct order."""
        app = AgentApp(name="TestApp")

        # Create agents that modify a shared counter
        call_order: list[str] = []

        class TrackingAgent(Agent):
            name: str
            order_list: list[str]

            def run(self, payload):
                self.order_list.append(self.name)
                return payload

        agent1 = TrackingAgent(name="First", order_list=call_order)
        agent2 = TrackingAgent(name="Second", order_list=call_order)
        agent3 = TrackingAgent(name="Third", order_list=call_order)

        app.add_agent(agent1)
        app.add_agent(agent2)
        app.add_agent(agent3)

        app.execute("test")

        assert agent1.order_list == ["First"]
        assert agent2.order_list == ["Second"]
        assert agent3.order_list == ["Third"]