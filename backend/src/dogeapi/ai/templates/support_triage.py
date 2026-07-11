"""Support-ticket triage agent.

Classifies an inbound support message into a category + severity, drafts a
suggested reply, and decides whether a human must take over. Two guardrails
make it production-grade rather than a demo:

- The system prompt tells the model to classify conservatively (pick the
  higher severity when torn) and to escalate billing disputes, security and
  legal topics.
- A deterministic output validator enforces a *severity floor*: severity-4
  results are always ``needs_human=True`` no matter what the model said.

Usage::

    from dogeapi.ai.templates import SupportContext, build_support_triage_agent
    from dogeapi.settings import get_settings

    agent = build_support_triage_agent(get_settings())
    result = await agent.run(
        "I was charged twice this month and need a refund NOW",
        deps=SupportContext(user_id="u_123", recent_tickets=["Duplicate charge in May"]),
    )
    result.output  # TriageResult

``SupportContext.recent_tickets`` is intentionally a plain sequence: wire it
to your real ticket store when you drop this in (see ``fetch_recent_tickets``
below), and the agent will spot repeat issues.

Note: this module deliberately avoids ``from __future__ import annotations``
&mdash; tool signatures must hold real types at runtime for Pydantic AI to
build their schemas, while ``pydantic_ai`` itself stays a lazy import so the
optional ``ai`` extra is only needed once you call the factory.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from dogeapi.ai.agents import build_agent

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from dogeapi.settings import Settings


TriageCategory = Literal["billing", "bug", "feature_request", "account", "other"]

SYSTEM_PROMPT = (
    "You triage inbound support messages for a multi-tenant SaaS product. "
    "Classify conservatively: if unsure between two severities, pick the higher. "
    "Severity scale: 1=question/info, 2=degraded experience, 3=feature broken, "
    "4=data loss, security incident, or money at risk. "
    "Set needs_human=true for anything touching billing disputes, security, or legal. "
    "The suggested_reply must be polite, specific to the message, and never promise "
    "refunds, credits, or timelines. "
    "Call fetch_recent_tickets before deciding: repeat issues raise severity."
)


class TriageResult(BaseModel):
    """Structured verdict for one inbound support message."""

    category: TriageCategory = Field(description="Primary topic of the message.")
    severity: int = Field(ge=1, le=4, description="1=info, 2=degraded, 3=broken, 4=data-loss/security/money.")
    summary: str = Field(max_length=200, description="One-line neutral restatement of the issue.")
    suggested_reply: str = Field(description="Draft first response an agent could send as-is.")
    needs_human: bool = Field(description="True when a human must review before any reply is sent.")


@dataclass
class SupportContext:
    """Per-request dependencies for the triage agent.

    Replace ``recent_tickets`` with a query against your ticket store; the
    default keeps the template runnable with zero infrastructure.
    """

    user_id: str
    recent_tickets: Sequence[str] = ()


def build_support_triage_agent(
    settings: "Settings",
    *,
    model: str | None = None,
) -> "Agent[SupportContext, TriageResult]":
    """Build the support-triage agent against the LLM Gateway.

    Raises :class:`dogeapi.ai.agents.GatewayNotConfiguredError` when
    ``LLM_GATEWAY_API_KEY`` is not configured.
    """
    from pydantic_ai import RunContext

    agent: Agent[SupportContext, TriageResult] = build_agent(
        settings,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        deps_type=SupportContext,
        output_type=TriageResult,
        retries=2,
    )

    @agent.tool
    async def fetch_recent_tickets(ctx: RunContext[SupportContext]) -> list[str]:
        """Recent ticket subjects for this user &mdash; lets the model spot repeat issues."""
        return list(ctx.deps.recent_tickets)

    @agent.output_validator
    async def enforce_severity_floor(ctx: RunContext[SupportContext], output: TriageResult) -> TriageResult:
        """Deterministic guardrail: severity-4 results always require a human."""
        if output.severity >= 4 and not output.needs_human:
            return output.model_copy(update={"needs_human": True})
        return output

    return agent


__all__ = (
    "SYSTEM_PROMPT",
    "SupportContext",
    "TriageCategory",
    "TriageResult",
    "build_support_triage_agent",
)
