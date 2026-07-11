"""Citation-enforcing RAG answerer.

Answers a question over a set of retrieved chunks and *refuses to emit
claims without a citation*: an output validator rejects any answer whose
``citations`` list is empty or references a chunk id that was not actually
retrieved, forcing the model to retry. When retrieval came back empty (or
irrelevant), the model must set ``insufficient_context=True`` instead of
improvising.

Retrieval is deliberately out of scope &mdash; run your vector/keyword search
first, then hand the chunks in via :class:`RagContext`. That keeps the
template storage-agnostic (pgvector, Qdrant, a SQL ``LIKE`` &mdash; anything).

Usage::

    from dogeapi.ai.templates import RagContext, RetrievedChunk, build_rag_answerer
    from dogeapi.settings import get_settings

    agent = build_rag_answerer(get_settings())
    result = await agent.run(
        "How do refunds work?",
        deps=RagContext(chunks=[RetrievedChunk(id="c1", source="docs/billing.md", text="...")]),
    )
    result.output  # CitedAnswer with citations drawn from the retrieved ids

Note: this module deliberately avoids ``from __future__ import annotations``
&mdash; see :mod:`dogeapi.ai.templates.support_triage` for why.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from dogeapi.ai.agents import build_agent

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from dogeapi.settings import Settings


SYSTEM_PROMPT = (
    "You answer questions using ONLY the context chunks provided in this conversation. "
    "Every factual claim must be supported by at least one chunk; list the supporting "
    "chunk ids in citations. If the chunks do not contain the answer, set "
    "insufficient_context=true, leave citations empty, and say briefly that the "
    "available documentation does not cover the question. Never use outside knowledge."
)


class RetrievedChunk(BaseModel):
    """One chunk returned by your retrieval step."""

    id: str = Field(description="Stable identifier the model cites, e.g. a chunk PK.")
    source: str = Field(description="Human-readable origin, e.g. 'docs/billing.md#refunds'.")
    text: str = Field(description="The chunk content shown to the model.")


@dataclass
class RagContext:
    """Per-request dependencies: the chunks your retriever found."""

    chunks: Sequence[RetrievedChunk] = ()


class CitedAnswer(BaseModel):
    """An answer that is only trusted as far as its citations."""

    answer: str = Field(description="The answer, grounded in the cited chunks.")
    citations: list[str] = Field(
        default_factory=list,
        description="Ids of the retrieved chunks that support the answer.",
    )
    insufficient_context: bool = Field(
        default=False,
        description="True when the retrieved chunks cannot answer the question.",
    )


def build_rag_answerer(
    settings: "Settings",
    *,
    model: str | None = None,
) -> "Agent[RagContext, CitedAnswer]":
    """Build the citation-enforcing RAG agent against the LLM Gateway.

    Raises :class:`dogeapi.ai.agents.GatewayNotConfiguredError` when
    ``LLM_GATEWAY_API_KEY`` is not configured.
    """
    from pydantic_ai import ModelRetry, RunContext

    agent: Agent[RagContext, CitedAnswer] = build_agent(
        settings,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=RagContext,
        output_type=CitedAnswer,
        retries=2,
    )

    @agent.system_prompt
    async def inject_chunks(ctx: RunContext[RagContext]) -> str:
        """Render the retrieved chunks into the prompt, ids first so they are easy to cite."""
        if not ctx.deps.chunks:
            return "No context chunks were retrieved for this question."
        rendered = "\n\n".join(f"[{chunk.id}] ({chunk.source})\n{chunk.text}" for chunk in ctx.deps.chunks)
        return f"Context chunks:\n\n{rendered}"

    @agent.output_validator
    async def require_citations(ctx: RunContext[RagContext], output: CitedAnswer) -> CitedAnswer:
        """Reject uncited or mis-cited answers so they never reach the caller."""
        known = {chunk.id for chunk in ctx.deps.chunks}

        if not known and not output.insufficient_context:
            raise ModelRetry("No chunks were retrieved: set insufficient_context=true instead of answering.")

        if output.insufficient_context:
            if output.citations:
                raise ModelRetry("insufficient_context=true must not carry citations.")
            return output

        if not output.citations:
            raise ModelRetry(
                "Every answer must cite at least one retrieved chunk id, or set insufficient_context=true."
            )

        unknown = sorted(set(output.citations) - known)
        if unknown:
            raise ModelRetry(f"Unknown citation ids {unknown}; cite only retrieved chunk ids: {sorted(known)}.")

        return output

    return agent


__all__ = (
    "SYSTEM_PROMPT",
    "CitedAnswer",
    "RagContext",
    "RetrievedChunk",
    "build_rag_answerer",
)
