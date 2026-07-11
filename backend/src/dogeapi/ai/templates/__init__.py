"""Production agent templates for the LLM Gateway (FEATURE_AI_CHAT).

Drop-in, MIT-licensed agents built on :func:`dogeapi.ai.agents.build_agent`.
Unlike :mod:`dogeapi.ai.examples` (minimal teaching demos), these are the
reliability layer: typed outputs, deterministic guardrail validators, and a
golden-transcript eval harness so you can customize a template and verify
nothing broke.

- :mod:`dogeapi.ai.templates.support_triage` &mdash; classify inbound support
  messages with a severity floor that always escalates to a human.
- :mod:`dogeapi.ai.templates.rag_answerer` &mdash; answer over retrieved chunks,
  refusing to emit claims without a citation from the retrieval set.
- :mod:`dogeapi.ai.templates.evals` &mdash; run golden transcripts against any
  agent in this package (or your own).

Like the rest of the ``ai`` module, nothing here imports ``pydantic_ai`` at
module scope &mdash; the optional ``ai`` extra is only required once you call
a ``build_*`` factory.
"""

from dogeapi.ai.templates.evals import (
    CaseResult,
    EvalReport,
    GoldenCase,
    load_golden_cases,
    run_golden_cases,
)
from dogeapi.ai.templates.rag_answerer import (
    CitedAnswer,
    RagContext,
    RetrievedChunk,
    build_rag_answerer,
)
from dogeapi.ai.templates.support_triage import (
    SupportContext,
    TriageResult,
    build_support_triage_agent,
)

__all__ = (
    "CaseResult",
    "CitedAnswer",
    "EvalReport",
    "GoldenCase",
    "RagContext",
    "RetrievedChunk",
    "SupportContext",
    "TriageResult",
    "build_rag_answerer",
    "build_support_triage_agent",
    "load_golden_cases",
    "run_golden_cases",
)
