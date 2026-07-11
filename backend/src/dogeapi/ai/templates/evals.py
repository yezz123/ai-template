"""Golden-transcript eval harness.

Runs a list of :class:`GoldenCase` prompts against any Pydantic AI agent
(the templates in this package, the examples, or your own) and reports
which cases still pass. The point is regression safety: customize a
template's prompt or tools, re-run the goldens, and know immediately
whether you broke behaviour you relied on.

Two kinds of checks per case, both optional:

- ``expected`` &mdash; exact-match assertions against fields of a structured
  output (dotted paths supported, e.g. ``"category"`` or ``"user.email"``).
- ``contains`` &mdash; substrings that must appear in the rendered output
  (useful for plain-text agents).

Cases can live in code or in JSON files (see ``golden/support_triage.json``
next to this module)::

    from dogeapi.ai.templates import build_support_triage_agent, load_golden_cases, run_golden_cases
    from dogeapi.settings import get_settings

    agent = build_support_triage_agent(get_settings())
    cases = load_golden_cases("src/dogeapi/ai/templates/golden/support_triage.json")
    report = await run_golden_cases(agent, cases, deps_factory=lambda case: SupportContext(user_id="eval"))
    assert report.ok, report.summary()

The harness never touches the network itself &mdash; whatever model the agent
is built against (live gateway, or a mock in tests) is what gets evaluated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic_ai import Agent


class GoldenCase(BaseModel):
    """One golden transcript: a prompt plus assertions about the output."""

    name: str = Field(description="Unique, human-readable case id.")
    prompt: str = Field(description="The user message sent to the agent.")
    expected: dict[str, Any] = Field(
        default_factory=dict,
        description="Dotted output-field path -> exact expected value.",
    )
    contains: list[str] = Field(
        default_factory=list,
        description="Substrings that must appear in the rendered output.",
    )


class CaseResult(BaseModel):
    """Outcome of one golden case."""

    name: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    output: str = Field(default="", description="Rendered agent output, for debugging failures.")


class EvalReport(BaseModel):
    """Aggregate outcome of a golden run."""

    results: list[CaseResult] = Field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        """One line per case, failures first &mdash; suitable for CI logs."""
        lines = [f"golden run: {self.passed}/{len(self.results)} passed"]
        for result in sorted(self.results, key=lambda r: r.passed):
            status = "PASS" if result.passed else "FAIL"
            lines.append(f"  [{status}] {result.name}")
            lines.extend(f"      - {failure}" for failure in result.failures)
        return "\n".join(lines)


def load_golden_cases(path: str | Path) -> list[GoldenCase]:
    """Load cases from a JSON file containing a list of GoldenCase objects."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level JSON list of golden cases.")
    return [GoldenCase.model_validate(item) for item in raw]


def _resolve(output: Any, dotted_path: str) -> Any:
    """Walk a dotted attribute path on the output (raises AttributeError if absent)."""
    value = output
    for part in dotted_path.split("."):
        value = getattr(value, part)
    return value


def _render(output: Any) -> str:
    """Human/CI-readable form of an agent output."""
    if isinstance(output, BaseModel):
        return output.model_dump_json()
    return str(output)


def _check_case(case: GoldenCase, output: Any) -> list[str]:
    """Return the list of assertion failures for one case (empty = pass)."""
    failures: list[str] = []
    for path, expected in case.expected.items():
        try:
            actual = _resolve(output, path)
        except AttributeError:
            failures.append(f"expected.{path}: field not present on output")
            continue
        if actual != expected:
            failures.append(f"expected.{path}: wanted {expected!r}, got {actual!r}")
    rendered = _render(output)
    failures.extend(f"contains: {needle!r} not found in output" for needle in case.contains if needle not in rendered)
    return failures


async def run_golden_cases(
    agent: Agent[Any, Any],
    cases: Sequence[GoldenCase],
    *,
    deps_factory: Callable[[GoldenCase], Any] | None = None,
) -> EvalReport:
    """Run every case against the agent and collect a report.

    Args:
        agent: Any Pydantic AI agent (typed output or plain text).
        cases: The golden cases to run.
        deps_factory: Builds the ``deps`` for each case when the agent
            requires them (e.g. ``lambda case: SupportContext(user_id="eval")``).

    Agent errors don't abort the run; they fail the individual case so one
    regression can't hide the others.
    """
    results: list[CaseResult] = []
    for case in cases:
        deps = deps_factory(case) if deps_factory is not None else None
        try:
            run_result = await agent.run(case.prompt, deps=deps)
        except Exception as exc:  # a crashed case is a failed case; keep running the rest
            results.append(CaseResult(name=case.name, passed=False, failures=[f"agent raised {exc!r}"]))
            continue
        failures = _check_case(case, run_result.output)
        results.append(
            CaseResult(
                name=case.name,
                passed=not failures,
                failures=failures,
                output=_render(run_result.output),
            )
        )
    return EvalReport(results=results)


__all__ = (
    "CaseResult",
    "EvalReport",
    "GoldenCase",
    "load_golden_cases",
    "run_golden_cases",
)
