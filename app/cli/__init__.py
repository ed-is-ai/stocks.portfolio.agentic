"""Command-line entry points, run as ``python -m app.cli.<module>``.

Living inside the ``app`` package (rather than a top-level ``scripts/``
directory) is what lets the documented invocation resolve ``app.*``
imports from a clean shell: this project has no ``[build-system]``, so it
is never installed, and ``python -m`` puts the working directory on
``sys.path`` while running a file by path does not (#480).
"""
