"""Service layer — business logic and orchestration.

Both entry points (the API and the scheduler) go through services rather than
touching agents, repositories, or SQL directly.
"""
