"""Workflow planning.

The model proposes a plan; the compiler decides whether it may exist. Two
guards sit between them, and the order matters:

1. **The candidate allowlist.** The model may only reference endpoint IDs that
   this run's searches actually returned. An ID outside that set is refused
   before the database is even consulted. Because IDs are opaque hashes, the
   model cannot construct a plausible one — the format itself does the work.

2. **The compiler.** Everything surviving step 1 still faces all seven
   deterministic checks.

When the compiler refuses a plan, its structured issues are handed back to the
model verbatim. That is the whole reason validation errors are typed objects
rather than sentences: the same payload drives the UI and the repair loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.orm import Session

from engine.agent.intent import Requirement, parse_intent
from engine.agent.llm import LLMClient, Usage
from engine.config import settings
from engine.search.retrieve import Candidate, search
from engine.workflow.compiler import compile_workflow
from engine.workflow.dag import (
    CompiledWorkflow,
    ValidationIssue,
    WorkflowDefinition,
)
from engine.workflow.schema_match import required_fields

log = logging.getLogger(__name__)

CANDIDATES_PER_SLOT = 8


@dataclass
class Attempt:
    """One pass through propose-then-validate."""

    iteration: int
    issues: list[ValidationIssue] = field(default_factory=list)
    raw: str = ""
    accepted: bool = False


@dataclass
class PlanOutcome:
    goal: str
    ok: bool
    requirement: Requirement | None = None
    definition: WorkflowDefinition | None = None
    compiled: CompiledWorkflow | None = None
    candidates: list[Candidate] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


SYSTEM_PROMPT = """You plan API workflows. You return ONLY a JSON object.

Shape:
{
  "name": "short workflow name",
  "description": "one sentence",
  "nodes": [
    {"id": "n1", "type": "trigger", "endpoint_id": "ep_...", "name": "...",
     "inputs": [{"target": "path.owner", "literal": "acme"}]},
    {"id": "n2", "type": "api_call", "endpoint_id": "ep_...", "name": "...",
     "inputs": [{"target": "body.text", "source_node": "n1",
                 "source_path": "title"}]}
  ],
  "edges": [{"source": "n1", "target": "n2"}]
}

If — and only if — the candidates cannot actually satisfy the goal, return this
instead, and nothing else:

{"cannot_satisfy": {"reason": "one sentence",
                    "missing": ["the capability or service that is absent"]}}

Use it when the goal names a service that is not among the candidates, or when
no candidate performs the operation asked for. A plan built from the nearest
available endpoints is worse than no plan: it looks correct, passes every
structural check, and does something nobody asked for.

ABSOLUTE RULES
1. endpoint_id MUST be copied exactly from the CANDIDATES list below. Never
   invent, guess, edit or abbreviate one. If nothing fits, say so with
   cannot_satisfy — do NOT substitute the nearest match.
2. Exactly one node has type "trigger". It is the workflow's ENTRY POINT, not
   necessarily an event: when the goal is something to do on demand, make the
   first API call itself the trigger node. Do not refuse a goal merely because
   no event-style endpoint exists. Every other node must be reachable from it
   through edges.
3. Every required parameter and required body field listed for a candidate MUST
   have an input. Use "literal" for constants, or "source_node" plus
   "source_path" to take a value from an earlier node's response.
4. "target" is location-prefixed: path.NAME, query.NAME, header.NAME or
   body.FIELD. Nested body fields use dots: body.message.text
5. "source_path" must be a field the source node's endpoint actually returns.
   Use the RETURNS list shown for each candidate. Never guess a field name.
6. Each input has EITHER literal OR (source_node and source_path). Never both.
7. Add no keys beyond those shown. Do not include method or path.

Prefer the fewest nodes that satisfy the goal."""


def _describe_candidate(candidate: Candidate, detail: dict[str, object]) -> str:
    lines = [
        f"- endpoint_id: {candidate.endpoint_id}",
        f"  {candidate.method} {candidate.path}  ({candidate.provider_id})",
    ]
    if candidate.summary:
        lines.append(f"  purpose: {candidate.summary[:110]}")
    required_params = detail.get("required_params") or []
    if required_params:
        lines.append(f"  REQUIRED params: {', '.join(required_params)}")
    required_body = detail.get("required_body") or []
    if required_body:
        lines.append(f"  REQUIRED body fields: {', '.join(required_body)}")
    returns = detail.get("returns") or []
    if returns:
        lines.append(f"  RETURNS: {', '.join(returns)}")
    return "\n".join(lines)


# Field names that usually carry the payload someone actually wants to move
# between steps. Real response objects have dozens of fields, and an arbitrary
# slice of them is worse than useless: Google Calendar events have forty, and
# taking the first four alphabetically showed "anyoneCanAddSelf" and hid both
# "summary" and "start", so the planner correctly concluded the data it needed
# was unavailable.
_INTERESTING = (
    "id",
    "name",
    "title",
    "subject",
    "summary",
    "description",
    "body",
    "text",
    "message",
    "content",
    "snippet",
    "email",
    "url",
    "link",
    "status",
    "state",
    "start",
    "end",
    "date",
    "time",
    "created",
    "updated",
    "timestamp",
    "author",
    "user",
    "sender",
    "from",
    "to",
    "recipient",
    "phone",
    "number",
    "amount",
    "total",
    "price",
    "currency",
    "label",
    "tag",
    "type",
    "key",
    "value",
)


def _field_rank(name: str) -> tuple[int, str]:
    """Order fields so the useful ones survive the budget."""
    lowered = name.lower()
    if lowered in _INTERESTING:
        return (0, lowered)
    if any(token in lowered for token in _INTERESTING):
        return (1, lowered)
    return (2, lowered)


def _describe_fields(
    properties: dict, prefix: str = "", depth: int = 0, budget: int = 40
) -> list[str]:
    """Readable field paths for one response schema.

    Arrays are shown with the `[]` marker the compiler already understands, so
    the model can write `messages[].id` and have it validate.

    Two properties matter as much as the format. The budget has to be large
    enough for real objects, and what it keeps has to be chosen rather than
    taken in whatever order the specification happened to list. The model can
    only name a field it was shown, so a preview that omits the obvious ones
    makes a possible workflow look impossible.
    """
    names: list[str] = []
    ordered = sorted(properties.items(), key=lambda item: _field_rank(item[0]))

    for name, sub in ordered:
        if len(names) >= budget:
            break
        path = f"{prefix}{name}"
        names.append(path)
        if not isinstance(sub, dict) or depth >= 2:
            continue

        nested_budget = max(3, (budget - len(names)) // 3)
        if isinstance(sub.get("properties"), dict):
            names.extend(_describe_fields(sub["properties"], f"{path}.", depth + 1, nested_budget))
        elif isinstance(sub.get("items"), dict):
            items = sub["items"]
            if isinstance(items.get("properties"), dict):
                names.extend(
                    _describe_fields(items["properties"], f"{path}[].", depth + 1, nested_budget)
                )
    return names[:budget]


def _candidate_details(session: Session, candidates: list[Candidate]) -> dict[str, dict]:
    """Load the facts the model needs to build a *valid* plan.

    Handing over required fields and available response fields up front is what
    turns most repair iterations into unnecessary ones — the model cannot supply
    a required parameter it was never told about.
    """
    from sqlalchemy import select

    from engine.db import Parameter, SchemaRecord

    ids = [c.endpoint_id for c in candidates]
    if not ids:
        return {}

    details: dict[str, dict] = {cid: {} for cid in ids}

    for parameter in session.scalars(select(Parameter).where(Parameter.endpoint_id.in_(ids))):
        if parameter.required:
            entry = details.setdefault(parameter.endpoint_id, {})
            entry.setdefault("required_params", []).append(f"{parameter.location}.{parameter.name}")

    for record in session.scalars(select(SchemaRecord).where(SchemaRecord.endpoint_id.in_(ids))):
        entry = details.setdefault(record.endpoint_id, {})
        schema = record.json_schema or {}
        if record.direction == "request":
            fields = required_fields(schema)
            if fields:
                entry["required_body"] = [f"body.{f}" for f in fields]
        elif record.direction == "response" and "returns" not in entry:
            properties = schema.get("properties")
            if isinstance(properties, dict):
                # The preview must show the paths the model is allowed to write,
                # including into arrays. Listing only "messages" for a response
                # shaped {messages: [{id, threadId}]} leaves no way to name the
                # id, so a perfectly possible workflow looks impossible.
                entry["returns"] = _describe_fields(properties)

    return details


def gather_candidates(
    session: Session, requirement: Requirement, *, per_slot: int = CANDIDATES_PER_SLOT
) -> list[Candidate]:
    """One search per capability the requirement names, then deduplicate."""
    seen: dict[str, Candidate] = {}
    for slot in requirement.search_slots:
        result = search(session, slot, mode="hybrid", limit=per_slot)
        for candidate in result.candidates:
            seen.setdefault(candidate.endpoint_id, candidate)
    log.info(
        "gathered %d unique candidates across %d slots", len(seen), len(requirement.search_slots)
    )
    return list(seen.values())


def _build_user_prompt(
    requirement: Requirement,
    candidates: list[Candidate],
    details: dict[str, dict],
    previous: str | None,
    issues: list[ValidationIssue],
) -> str:
    catalogue = "\n".join(
        _describe_candidate(c, details.get(c.endpoint_id, {})) for c in candidates
    )
    sections = [
        f"GOAL: {requirement.goal}",
        f"TRIGGER: {requirement.trigger}",
        f"ACTIONS: {', '.join(requirement.actions) or '(none)'}",
        f"DATA THAT MUST FLOW: {', '.join(requirement.required_fields) or '(none)'}",
        "",
        "CANDIDATES (the only endpoint_ids you may use):",
        catalogue,
    ]
    if previous and issues:
        rendered = "\n".join(
            f"- [{i.code}] node={i.node_id or '-'} field={i.field or '-'}: {i.message}"
            + (f"  HINT: {i.hint}" if i.hint else "")
            for i in issues
        )
        sections += [
            "",
            "YOUR PREVIOUS PLAN WAS REJECTED:",
            previous,
            "",
            "VALIDATION ERRORS — fix every one:",
            rendered,
        ]
    sections += ["", "Return the corrected JSON workflow object."]
    return "\n".join(sections)


def _fabricated_id_issues(
    definition: WorkflowDefinition, allowed: set[str]
) -> list[ValidationIssue]:
    """Reject any endpoint the model did not receive in its candidate list."""
    issues: list[ValidationIssue] = []
    for node in definition.nodes:
        if node.endpoint_id and node.endpoint_id not in allowed:
            issues.append(
                ValidationIssue(
                    code="endpoint_not_in_candidates",
                    node_id=node.id,
                    field="endpoint_id",
                    actual=node.endpoint_id,
                    message=f"endpoint_id {node.endpoint_id!r} was not in the candidate list",
                    hint="copy an endpoint_id exactly from CANDIDATES; never construct one",
                )
            )
    return issues


def plan_workflow(
    session: Session,
    goal: str,
    client: LLMClient,
    *,
    max_repairs: int | None = None,
    enforce_allowlist: bool = False,
) -> PlanOutcome:
    """Turn a natural-language goal into a validated, compiled workflow.

    `enforce_allowlist` defaults to False because planning spans every indexed
    provider; the execution allowlist is a runtime restriction, applied when a
    workflow is actually run.
    """
    if max_repairs is None:
        max_repairs = settings.planner_max_repair_iterations

    outcome = PlanOutcome(goal=goal, ok=False)

    requirement, completion = parse_intent(client, goal)
    outcome.requirement = requirement
    outcome.usage.add(completion.usage)

    candidates = gather_candidates(session, requirement)
    outcome.candidates = candidates
    if not candidates:
        outcome.issues = [
            ValidationIssue(
                code="no_candidates",
                message="search returned no endpoints for this goal",
                hint="try naming the capability more concretely",
            )
        ]
        return outcome

    allowed = {c.endpoint_id for c in candidates}
    details = _candidate_details(session, candidates)
    previous_raw: str | None = None
    issues: list[ValidationIssue] = []

    for iteration in range(1, max_repairs + 2):
        user = _build_user_prompt(requirement, candidates, details, previous_raw, issues)
        completion = client.complete_json(system=SYSTEM_PROMPT, user=user, max_tokens=8192)
        outcome.usage.add(completion.usage)
        previous_raw = completion.raw
        attempt = Attempt(iteration=iteration, raw=completion.raw)

        # An explicit "this cannot be done" is a legitimate answer, and a far
        # better one than a workflow assembled from whatever happened to rank
        # highest. Reported once and not retried: repeating the same search
        # would return the same candidates.
        if isinstance(completion.data.get("cannot_satisfy"), dict):
            refusal = completion.data["cannot_satisfy"]
            missing = refusal.get("missing") or []
            outcome.attempts.append(attempt)
            outcome.issues = [
                ValidationIssue(
                    code="no_suitable_endpoint",
                    message=str(refusal.get("reason") or "no candidate endpoint fits this goal"),
                    actual=", ".join(str(m) for m in missing) or None,
                    hint="the knowledge base has no API for this. Ingest more "
                    "specifications, or describe the goal using a service that is indexed.",
                )
            ]
            log.info("planner reported the goal cannot be satisfied: %s", missing)
            return outcome

        # Shape check first: a plan that is not even the right shape cannot be
        # meaningfully validated, and the parse error is the useful feedback.
        try:
            definition = WorkflowDefinition.model_validate(completion.data)
        except ValidationError as err:
            issues = [
                ValidationIssue(
                    code="malformed_plan",
                    message=f"plan did not match the required shape: {err}",
                    hint="return exactly the JSON shape shown in the instructions",
                )
            ]
            attempt.issues = issues
            outcome.attempts.append(attempt)
            log.info("attempt %d: malformed plan", iteration)
            continue

        # Then the allowlist, before touching the database.
        issues = _fabricated_id_issues(definition, allowed)
        if not issues:
            result = compile_workflow(session, definition, enforce_allowlist=enforce_allowlist)
            if result.ok:
                attempt.accepted = True
                outcome.attempts.append(attempt)
                outcome.ok = True
                outcome.definition = definition
                outcome.compiled = result.workflow
                log.info(
                    "plan accepted on attempt %d (%d tokens)", iteration, outcome.usage.total_tokens
                )
                return outcome
            issues = result.issues

        attempt.issues = issues
        outcome.attempts.append(attempt)
        log.info("attempt %d rejected: %s", iteration, [i.code for i in issues])

    outcome.issues = issues
    return outcome
