"""Intent parsing: a sentence becomes a structured requirement.

The output is a typed object, not free text. Everything downstream — retrieval,
planning, validation — reads fields rather than re-interpreting prose, so the
model's understanding is committed to once and then checked, instead of being
re-derived (and re-hallucinated) at every stage.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.agent.llm import Completion, LLMClient, LLMError

log = logging.getLogger(__name__)


class Requirement(BaseModel):
    """What the user asked for, in a shape the rest of the engine can act on."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    trigger: str = Field(description="the event that starts the automation")
    actions: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    needs_auth: bool = True

    @property
    def is_manual(self) -> bool:
        return self.trigger.strip().lower() in ("manual", "none", "on demand", "")

    @property
    def search_slots(self) -> list[str]:
        """The capabilities to search for, one retrieval per slot.

        A manual trigger is not a capability, so it is not searched for —
        looking up "manual" would spend a retrieval slot returning noise.
        """
        slots = list(self.actions) if self.is_manual else [self.trigger, *self.actions]
        return [slot for slot in slots if slot]


SYSTEM_PROMPT = """You convert automation requests into structured requirements.

Return ONLY a JSON object with exactly these keys:
  goal            - one sentence restating the request
  trigger         - the event that starts it, as a short capability phrase,
                    or exactly "manual" when the request describes something to
                    do on demand rather than something to react to
  actions         - list of capability phrases for what should happen, in order
  required_fields - list of data fields that must flow through, dotted if nested
  constraints     - list of conditions or limits; empty list if none
  needs_auth      - true if any step needs credentials

Write trigger and actions the way API documentation would describe an
operation, not the way a user speaks — but ALWAYS keep the name of any service
the request mentions. The service name is the strongest clue for finding the
right API, and dropping it makes the search look for the operation among every
provider at once.

  "Slack me when a GitHub issue opens"
    trigger: "a new issue is created in a GitHub repository"
    actions: ["post a message to a Slack channel"]

  "whenever I get a Gmail, send me a WhatsApp message"
    trigger: "receive a new email in Gmail"
    actions: ["send a WhatsApp message"]

Not every request has an event. "Read my latest email and send it on" is a
sequence of actions performed when asked; its trigger is "manual". Inventing an
event for such a request sends the search hunting for a trigger endpoint that
was never wanted. Use "manual" whenever the request does not clearly say
"when", "whenever" or "every time".

Keep each phrase under ten words. Do not introduce a service the request never
mentions. Do not invent fields the request never mentions. Add no keys beyond
the eight listed."""


USER_TEMPLATE = """Automation request:
{goal}

Return the JSON object."""


def parse_intent(client: LLMClient, goal: str) -> tuple[Requirement, Completion]:
    """Turn a natural-language goal into a Requirement.

    A response that does not fit the schema is retried once with the validation
    error appended — cheaper than failing, and the second attempt almost always
    succeeds because the error names the exact problem.
    """
    user = USER_TEMPLATE.format(goal=goal.strip())
    completion = client.complete_json(system=SYSTEM_PROMPT, user=user, max_tokens=1024)

    try:
        requirement = Requirement.model_validate({"goal": goal, **completion.data})
    except ValidationError as first_error:
        log.info("intent response rejected, retrying once: %s", first_error)
        retry_user = (
            f"{user}\n\nYour previous response was rejected:\n{first_error}\n"
            "Return a corrected JSON object with exactly the required keys."
        )
        completion = client.complete_json(system=SYSTEM_PROMPT, user=retry_user, max_tokens=1024)
        try:
            requirement = Requirement.model_validate({"goal": goal, **completion.data})
        except ValidationError as second_error:
            raise LLMError(f"intent parsing failed twice: {second_error}") from second_error

    log.info(
        "intent: trigger=%r actions=%s fields=%s",
        requirement.trigger,
        requirement.actions,
        requirement.required_fields,
    )
    return requirement, completion
