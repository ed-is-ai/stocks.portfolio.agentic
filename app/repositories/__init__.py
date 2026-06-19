"""Repository layer — the sole owner of database access.

Each repository wraps one SQLite database (``trades.db``, ``alerts.db``,
``results.db``) or the on-disk artifact files (CSV/JSON). Repositories take a
connection factory and never import agents, services, api, or workflows.
"""
