"""The built-in SRE agents, their result schemas, and how a workspace spec overrides them.

    triage ──(needs investigation)──► investigator ──delegates──► data_analyst / history_analyst

Result schemas are deliberately flat and small: they are the contract with the model (small local models included)
and what channels and the timeline display.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field

from sentinel.agents.runtime import AgentDefinition
from sentinel.core.spec import AgentSpec, Budget


class TriageResult(BaseModel):
    severity: Literal["info", "low", "medium", "high", "critical"] = Field(
        description="Your assessment of how serious this is for the business")
    business_impact: str = Field(description="1-3 plain sentences: who or what is affected, in business terms")
    likely_cause: str = Field(description="Best current hypothesis, marked as a hypothesis")
    duplicate_of: str | None = Field(default=None, description="Id of an earlier incident with the same cause, if any")
    needs_investigation: bool = Field(description="True unless the cause is already clear or it is noise")


class Finding(BaseModel):
    claim: str = Field(description="One verified fact")
    evidence: str = Field(description="Where it was seen: tool, query, table, log line")


class InvestigationResult(BaseModel):
    root_cause: str = Field(description="The most likely root cause, 1-3 sentences")
    confidence: Literal["low", "medium", "high"]
    findings: list[Finding] = Field(default_factory=list, description="Up to 6 verified facts supporting it")
    blast_radius: str = Field(default="", description="Downstream tables, dashboards, teams affected")
    recommended_actions: list[str] = Field(default_factory=list, description="Concrete next steps, most important first")
    suggested_action_id: str | None = Field(
        default=None, description="If one of the workspace's pre-approved actions fixes this, its id")


class SpecialistReport(BaseModel):
    answer: str = Field(description="Direct answer to the question asked")
    findings: list[Finding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, description="What could not be verified")


TRIAGE = AgentDefinition(
    name="triage",
    description="Assesses a new incident quickly.",
    instructions="""You are the triage agent of an AI SRE team. A new incident just opened.
Within a few tool calls: check whether this matches a recent incident (same fingerprint or service), judge the
business impact using the organization context, and form a first hypothesis. Decide whether a deeper investigation
is warranted. Be fast and calibrated: mark hypotheses as hypotheses.""",
    output_model=TriageResult,
    model_role="fast",
    toolsets=("incident",),
    budget=Budget(max_steps=6, max_tokens=60_000, max_seconds=120, max_cost_usd=0.25),
)

INVESTIGATOR = AgentDefinition(
    name="investigator",
    description="Finds the root cause of an incident.",
    instructions="""You are the lead investigator of an AI SRE team. Find the root cause of the incident.
Work hypothesis-first: state what would confirm or rule out each hypothesis, then check it. Delegate focused
questions to specialists when their tools fit (data and pipeline questions to the data analyst, past incidents to
the history analyst); you can call several specialists in one turn. Converge on one root cause, list the evidence,
the blast radius and the recommended actions. If a pre-approved action in the context fits, name its id.""",
    output_model=InvestigationResult,
    model_role="smart",
    toolsets=("incident",),
    subagents=("data_analyst", "history_analyst"),
    budget=Budget(max_steps=12, max_tokens=250_000, max_seconds=300, max_cost_usd=1.50),
)

DATA_ANALYST = AgentDefinition(
    name="data_analyst",
    description="Answers questions with read-only SQL over the data warehouse (pipeline runs, query costs, lineage).",
    instructions="""You are a data analyst on an AI SRE team with read-only SQL access to the warehouse.
Answer the question you were given. Discover before you query: list schemas and tables, describe a table before
selecting from it, and always filter and limit. Report exact numbers and the queries that produced them.""",
    output_model=SpecialistReport,
    model_role="fast",
    toolsets=("exasol",),
    budget=Budget(max_steps=10, max_tokens=120_000, max_seconds=180, max_cost_usd=0.50),
)

HISTORY_ANALYST = AgentDefinition(
    name="history_analyst",
    description="Looks up past incidents, their timelines and how they ended.",
    instructions="""You are the incident historian of an AI SRE team. Find past incidents related to the question,
how they were resolved, and whether this looks like a repeat. Cite incident ids.""",
    output_model=SpecialistReport,
    model_role="fast",
    toolsets=("incident",),
    budget=Budget(max_steps=6, max_tokens=60_000, max_seconds=120, max_cost_usd=0.25),
)

BUILTIN: dict[str, AgentDefinition] = {a.name: a for a in (TRIAGE, INVESTIGATOR, DATA_ANALYST, HISTORY_ANALYST)}


def resolve_definitions(overrides: Iterable[AgentSpec], available_toolsets: set[str]) -> dict[str, AgentDefinition]:
    """Built-ins, with the workspace's agents applied on top. An override names a built-in (or `extends` one) and
    changes only what it sets; a new agent must extend a built-in to inherit its result schema."""
    agents = dict(BUILTIN)
    for spec in overrides:
        base = agents.get(spec.extends or spec.name)
        if base is None:
            raise ValueError(f"agent '{spec.name}' must extend one of: {', '.join(sorted(agents))}")
        changes: dict = {"name": spec.name}
        if spec.instructions:
            extra = spec.instructions.strip()
            changes["instructions"] = f"{base.instructions}\n\nTeam-specific guidance:\n{extra}"
        if spec.model:
            changes["model_role"] = spec.model
        if spec.toolsets is not None:
            changes["toolsets"] = tuple(spec.toolsets)
        if spec.subagents is not None:
            changes["subagents"] = tuple(spec.subagents)
        if spec.budget is not None:
            changes["budget"] = spec.budget
        agents[spec.name] = replace(base, **changes)
    # Drop toolsets that are not configured here, so an agent never sees tools that cannot work.
    return {name: replace(a, toolsets=tuple(t for t in a.toolsets if t in available_toolsets))
            for name, a in agents.items()}
