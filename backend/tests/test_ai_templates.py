"""Tests for the production agent templates (dogeapi.ai.templates).

Same approach as ``test_pydantic_agent.py``: the gateway is mocked over HTTP
with respx so everything runs offline. Each template's guardrail is exercised
directly:

- support triage: structured output parses, and the deterministic severity
  floor flips ``needs_human`` on severity-4 results.
- RAG answerer: an uncited/mis-cited answer is rejected and retried; an empty
  retrieval set forces ``insufficient_context``.
- eval harness: golden cases pass/fail independently, agent crashes fail the
  single case, and the shipped sample goldens load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import dogeapi.ai.templates as templates_pkg
from dogeapi.ai.templates import (
    CitedAnswer,
    GoldenCase,
    RagContext,
    RetrievedChunk,
    SupportContext,
    TriageResult,
    build_rag_answerer,
    build_support_triage_agent,
    load_golden_cases,
    run_golden_cases,
)
from dogeapi.settings import Settings

GATEWAY_BASE = "https://api.llmgateway.io/v1"


def _settings(api_key: str = "test-key", base_url: str = GATEWAY_BASE) -> Settings:
    return Settings(
        JWT_SECRET_KEY="x" * 32,
        LLM_GATEWAY_URL=base_url,
        LLM_GATEWAY_API_KEY=api_key,
        AI_DEFAULT_MODEL="gpt-5-mini",
    )


def _structured_response(arguments: dict[str, Any], *, response_id: str = "resp-1") -> httpx.Response:
    """A chat completion whose only content is a ``final_result`` tool call."""
    return httpx.Response(
        200,
        json={
            "id": response_id,
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "gpt-5-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call-{response_id}",
                                "type": "function",
                                "function": {
                                    "name": "final_result",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
            },
        },
    )


# ─── Support triage ──────────────────────────────────────────────────────


@respx.mock
async def test_support_triage_parses_structured_output() -> None:
    settings = _settings()
    respx.post(f"{GATEWAY_BASE}/chat/completions").mock(
        return_value=_structured_response(
            {
                "category": "billing",
                "severity": 3,
                "summary": "Customer was charged twice this month.",
                "suggested_reply": "Sorry about that - we're looking into the duplicate charge now.",
                "needs_human": True,
            }
        )
    )

    agent = build_support_triage_agent(settings)
    result = await agent.run(
        "I was charged twice this month!",
        deps=SupportContext(user_id="u_1", recent_tickets=["Duplicate charge in May"]),
    )

    assert isinstance(result.output, TriageResult)
    assert result.output.category == "billing"
    assert result.output.severity == 3
    assert result.output.needs_human is True


@respx.mock
async def test_support_triage_severity_floor_forces_human() -> None:
    """A severity-4 verdict with needs_human=false must be corrected deterministically."""
    settings = _settings()
    respx.post(f"{GATEWAY_BASE}/chat/completions").mock(
        return_value=_structured_response(
            {
                "category": "bug",
                "severity": 4,
                "summary": "All org documents disappeared after sync.",
                "suggested_reply": "We are escalating this immediately.",
                "needs_human": False,
            }
        )
    )

    agent = build_support_triage_agent(settings)
    result = await agent.run(
        "All of our documents disappeared after the last sync.",
        deps=SupportContext(user_id="u_2"),
    )

    assert result.output.severity == 4
    assert result.output.needs_human is True


# ─── RAG answerer ────────────────────────────────────────────────────────

CHUNKS = [
    RetrievedChunk(id="c1", source="docs/billing.md#refunds", text="Refunds are issued within 5 business days."),
    RetrievedChunk(id="c2", source="docs/billing.md#invoices", text="Invoices are emailed monthly."),
]


@respx.mock
async def test_rag_answerer_accepts_cited_answer() -> None:
    settings = _settings()
    respx.post(f"{GATEWAY_BASE}/chat/completions").mock(
        return_value=_structured_response(
            {
                "answer": "Refunds are issued within 5 business days.",
                "citations": ["c1"],
                "insufficient_context": False,
            }
        )
    )

    agent = build_rag_answerer(settings)
    result = await agent.run("How long do refunds take?", deps=RagContext(chunks=CHUNKS))

    assert isinstance(result.output, CitedAnswer)
    assert result.output.citations == ["c1"]


@respx.mock
async def test_rag_answerer_retries_unknown_citation() -> None:
    """A citation outside the retrieved set is rejected; the retry must succeed."""
    settings = _settings()
    responses = iter(
        [
            _structured_response(
                {"answer": "Refunds take 5 days.", "citations": ["made-up-id"], "insufficient_context": False},
                response_id="resp-bad",
            ),
            _structured_response(
                {"answer": "Refunds take 5 business days.", "citations": ["c1"], "insufficient_context": False},
                response_id="resp-good",
            ),
        ]
    )
    route = respx.post(f"{GATEWAY_BASE}/chat/completions").mock(side_effect=lambda _request: next(responses))

    agent = build_rag_answerer(settings)
    result = await agent.run("How long do refunds take?", deps=RagContext(chunks=CHUNKS))

    assert route.call_count == 2
    assert result.output.citations == ["c1"]


@respx.mock
async def test_rag_answerer_requires_insufficient_context_when_no_chunks() -> None:
    """With an empty retrieval set, an improvised answer is rejected until the model concedes."""
    settings = _settings()
    responses = iter(
        [
            _structured_response(
                {"answer": "Refunds take about a week, usually.", "citations": [], "insufficient_context": False},
                response_id="resp-improvised",
            ),
            _structured_response(
                {
                    "answer": "The available documentation does not cover refunds.",
                    "citations": [],
                    "insufficient_context": True,
                },
                response_id="resp-conceded",
            ),
        ]
    )
    route = respx.post(f"{GATEWAY_BASE}/chat/completions").mock(side_effect=lambda _request: next(responses))

    agent = build_rag_answerer(settings)
    result = await agent.run("How long do refunds take?", deps=RagContext(chunks=[]))

    assert route.call_count == 2
    assert result.output.insufficient_context is True
    assert result.output.citations == []


# ─── Eval harness ────────────────────────────────────────────────────────


@respx.mock
async def test_eval_harness_reports_pass_and_fail_independently() -> None:
    settings = _settings()
    respx.post(f"{GATEWAY_BASE}/chat/completions").mock(
        return_value=_structured_response(
            {
                "category": "billing",
                "severity": 3,
                "summary": "Duplicate charge reported.",
                "suggested_reply": "We're checking the duplicate charge now.",
                "needs_human": True,
            }
        )
    )

    agent = build_support_triage_agent(settings)
    cases = [
        GoldenCase(
            name="passes",
            prompt="I was charged twice",
            expected={"category": "billing", "needs_human": True},
            contains=["duplicate charge"],
        ),
        GoldenCase(name="fails", prompt="I was charged twice", expected={"category": "bug"}),
    ]

    report = await run_golden_cases(agent, cases, deps_factory=lambda case: SupportContext(user_id="eval"))

    assert report.passed == 1
    assert report.failed == 1
    assert not report.ok
    by_name = {r.name: r for r in report.results}
    assert by_name["passes"].passed
    assert by_name["fails"].failures == ["expected.category: wanted 'bug', got 'billing'"]
    assert "[FAIL] fails" in report.summary()


@respx.mock
async def test_eval_harness_survives_agent_errors() -> None:
    """A gateway blow-up fails that one case instead of aborting the run."""
    settings = _settings()
    respx.post(f"{GATEWAY_BASE}/chat/completions").mock(return_value=httpx.Response(500, text="boom"))

    agent = build_support_triage_agent(settings)
    report = await run_golden_cases(
        agent,
        [GoldenCase(name="crashes", prompt="hello", expected={"category": "other"})],
        deps_factory=lambda case: SupportContext(user_id="eval"),
    )

    assert report.failed == 1
    assert "agent raised" in report.results[0].failures[0]


def test_shipped_golden_sample_loads() -> None:
    path = Path(templates_pkg.__file__).parent / "golden" / "support_triage.json"
    cases = load_golden_cases(path)

    assert len(cases) == 3
    assert len({case.name for case in cases}) == 3


def test_load_golden_cases_rejects_non_list(tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.json"
    bogus.write_text('{"name": "not-a-list"}', encoding="utf-8")
    with pytest.raises(ValueError, match="top-level JSON list"):
        load_golden_cases(bogus)
