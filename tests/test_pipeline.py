"""Unit tests for the typed linear Pipeline (app.workflows.pipeline)."""

from app.workflows.pipeline import Pipeline


class _Double:
    """Step that doubles an int."""

    name = "double"

    def run(self, payload: int) -> int:
        return payload * 2


class _ToStr:
    """Step that stringifies an int."""

    name = "to_str"

    def run(self, payload: int) -> str:
        return f"<{payload}>"


class _Exclaim:
    """Step that appends '!' to a string."""

    name = "exclaim"

    def run(self, payload: str) -> str:
        return payload + "!"


def test_run_returns_final_output() -> None:
    pipe = Pipeline.start(_Double()).then(_ToStr()).then(_Exclaim())
    assert pipe.run(5) == "<10>!"


def test_run_single_step() -> None:
    pipe = Pipeline.start(_Double())
    assert pipe.run(21) == 42


def test_run_traced_exposes_every_stage() -> None:
    pipe = Pipeline.start(_Double()).then(_ToStr()).then(_Exclaim())
    final, trace = pipe.run_traced(5)

    assert final == "<10>!"
    assert [name for name, _ in trace] == ["double", "to_str", "exclaim"]
    assert [out for _, out in trace] == [10, "<10>", "<10>!"]


def test_then_is_immutable() -> None:
    """`.then` returns a new pipeline; the original is unchanged."""
    base = Pipeline.start(_Double())
    extended = base.then(_ToStr())

    assert base.run(3) == 6
    assert extended.run(3) == "<6>"
