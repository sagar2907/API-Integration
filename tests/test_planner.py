"""Planner tests.

Every test uses a stubbed model with scripted responses. Real-model behaviour is
non-deterministic and costs money; it belongs in the benchmark suite, not here.
What these tests pin down is the machinery around the model — the candidate
allowlist, the repair loop, and the fact that nothing escapes the compiler.
"""

from __future__ import annotations

import pytest

from engine.agent.intent import Requirement, parse_intent
from engine.agent.llm import LLMError, StubClient, _extract_json
from engine.agent.planner import plan_workflow
from engine.workflow.dag import Code

INTENT_RESPONSE = {
    "goal": "post to slack when a github issue is opened",
    "trigger": "create an issue in a repository",
    "actions": ["post a message to a channel"],
    "required_fields": ["title"],
    "constraints": [],
    "needs_auth": True,
}


def valid_plan(text_source: str = "title", slack_id: str = "ep_slack_post") -> dict:
    return {
        "name": "github issue to slack",
        "description": "notify the team",
        "nodes": [
            {
                "id": "n1",
                "type": "trigger",
                "endpoint_id": "ep_gh_issue_create",
                "name": "New issue",
                "inputs": [
                    {"target": "path.owner", "literal": "acme"},
                    {"target": "path.repo", "literal": "web"},
                    {"target": "body.title", "literal": "placeholder"},
                ],
            },
            {
                "id": "n2",
                "type": "api_call",
                "endpoint_id": slack_id,
                "name": "Post to Slack",
                "inputs": [
                    {"target": "header.token", "literal": "xoxb"},
                    {"target": "body.channel", "literal": "#eng"},
                    {"target": "body.text", "source_node": "n1", "source_path": text_source},
                ],
            },
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }


@pytest.fixture(autouse=True)
def _stub_search(monkeypatch, session):
    """Return the fixture endpoints as candidates without touching the index.

    The search engine has its own tests and its own benchmark; wiring a real
    FTS index into planner tests would make them slow and couple two components
    that should fail independently.
    """
    from engine.agent import planner
    from engine.search.retrieve import Candidate

    def fake_gather(_session, _requirement, per_slot=8):
        return [
            Candidate(
                endpoint_id="ep_gh_issue_create",
                method="POST",
                path="/repos/{owner}/{repo}/issues",
                summary="Create an issue",
                operation_id="issues/create",
                api_name="GitHub",
                provider_id="github_com",
                provider_name="github_com",
                is_deprecated=False,
                is_destructive=False,
                score=1.0,
                rank=1,
            ),
            Candidate(
                endpoint_id="ep_slack_post",
                method="POST",
                path="/chat.postMessage",
                summary="Post a message",
                operation_id="chat_postMessage",
                api_name="Slack",
                provider_id="slack_com",
                provider_name="slack_com",
                is_deprecated=False,
                is_destructive=False,
                score=1.0,
                rank=2,
            ),
        ]

    monkeypatch.setattr(planner, "gather_candidates", fake_gather)


class TestIntentParsing:
    def test_produces_a_typed_requirement(self):
        client = StubClient([INTENT_RESPONSE])
        requirement, _ = parse_intent(client, "slack me when a github issue opens")
        assert requirement.trigger == "create an issue in a repository"
        assert requirement.actions == ["post a message to a channel"]

    def test_search_slots_are_the_trigger_plus_actions(self):
        requirement = Requirement(**INTENT_RESPONSE)
        assert requirement.search_slots == [
            "create an issue in a repository",
            "post a message to a channel",
        ]

    def test_a_malformed_response_is_retried_once(self):
        client = StubClient([{"trigger": 42}, INTENT_RESPONSE])
        requirement, _ = parse_intent(client, "anything")
        assert requirement.trigger == "create an issue in a repository"
        assert len(client.prompts) == 2
        # The retry must tell the model what was wrong, not just ask again.
        assert "rejected" in client.prompts[1][1]

    def test_failing_twice_raises(self):
        client = StubClient([{"trigger": 42}, {"trigger": 43}])
        with pytest.raises(LLMError, match="twice"):
            parse_intent(client, "anything")

    def test_extra_keys_are_refused(self):
        # extra="forbid" is what stops invented structure entering the pipeline.
        client = StubClient([{**INTENT_RESPONSE, "surprise": 1}, INTENT_RESPONSE])
        parse_intent(client, "anything")
        assert len(client.prompts) == 2


class TestIntentPromptKeepsServiceNames:
    """A named service must survive into the search query.

    The prompt originally illustrated the rule with "Slack me" becoming "post a
    message to a channel", which deletes the single most discriminating term.
    Retrieval was then left hunting for a generic operation across every
    provider, and Gmail and WhatsApp went unfound despite being indexed.
    """

    def test_the_prompt_instructs_the_model_to_keep_them(self):
        from engine.agent.intent import SYSTEM_PROMPT

        assert "ALWAYS keep the name of any service" in SYSTEM_PROMPT
        # The worked examples must demonstrate the rule, not contradict it.
        assert "post a message to a Slack channel" in SYSTEM_PROMPT
        assert "send a WhatsApp message" in SYSTEM_PROMPT

    def test_named_services_reach_the_search_slots(self):
        from engine.agent.intent import Requirement

        requirement = Requirement(
            goal="whenever I get a Gmail, send me a WhatsApp message",
            trigger="receive a new email in Gmail",
            actions=["send a WhatsApp message"],
        )
        slots = requirement.search_slots
        assert any("Gmail" in slot for slot in slots)
        assert any("WhatsApp" in slot for slot in slots)


class TestFieldPreview:
    """The model can only name paths it was shown.

    Gmail's list-messages returns {messages: [{id, threadId}]}. A preview that
    listed only "messages" gave no way to name the id, and the planner
    concluded — reasonably — that the workflow was impossible.
    """

    def test_array_item_fields_are_shown_with_the_bracket_marker(self):
        from engine.agent.planner import _describe_fields

        fields = _describe_fields(
            {
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "threadId": {"type": "string"}},
                    },
                }
            }
        )
        assert "messages" in fields
        assert "messages[].id" in fields

    def test_useful_fields_survive_a_tight_budget(self):
        from engine.agent.planner import _describe_fields

        # A real Google Calendar event has forty fields. Taking them in
        # specification order showed "anyoneCanAddSelf" and hid "summary",
        # which made a perfectly possible workflow look impossible.
        event = {f"zzFiller{i}": {"type": "string"} for i in range(40)}
        event.update(
            {
                "summary": {"type": "string"},
                "anyoneCanAddSelf": {"type": "boolean"},
                "start": {"type": "object", "properties": {"dateTime": {"type": "string"}}},
            }
        )
        fields = _describe_fields({"items": {"type": "array", "items": {"properties": event}}})
        assert "items[].summary" in fields
        assert "items[].start" in fields

    def test_deeply_nested_values_are_reachable(self):
        from engine.agent.planner import _describe_fields

        fields = _describe_fields(
            {
                "items": {
                    "type": "array",
                    "items": {
                        "properties": {
                            "start": {
                                "type": "object",
                                "properties": {"dateTime": {"type": "string"}},
                            }
                        }
                    },
                }
            }
        )
        assert "items[].start.dateTime" in fields

    def test_nested_object_fields_are_still_shown(self):
        from engine.agent.planner import _describe_fields

        fields = _describe_fields(
            {"user": {"type": "object", "properties": {"login": {"type": "string"}}}}
        )
        assert "user.login" in fields

    def test_the_preview_paths_resolve_against_the_real_schema(self):
        # A preview that showed unresolvable paths would be worse than none.
        from engine.agent.planner import _describe_fields
        from engine.workflow.schema_match import resolve_path

        schema = {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"id": {"type": "string"}}},
                }
            },
        }
        for path in _describe_fields(schema["properties"]):
            assert resolve_path(schema, path) is not None, path


class TestManualTriggers:
    """Not every automation reacts to an event.

    The requirement schema always demanded a trigger, so the model invented one
    for on-demand goals — and the planner then refused because no event
    endpoint existed. "Do A then B when I ask" is a legitimate workflow.
    """

    def test_manual_is_recognised(self):
        from engine.agent.intent import Requirement

        assert Requirement(goal="g", trigger="manual", actions=["send"]).is_manual
        assert not Requirement(goal="g", trigger="a new issue is created").is_manual

    def test_a_manual_trigger_is_not_searched_for(self):
        from engine.agent.intent import Requirement

        requirement = Requirement(
            goal="read my mail then message me",
            trigger="manual",
            actions=["read the latest message in Gmail", "send a WhatsApp message"],
        )
        # Searching for the word "manual" would waste a retrieval slot on noise.
        assert requirement.search_slots == [
            "read the latest message in Gmail",
            "send a WhatsApp message",
        ]

    def test_the_prompt_explains_when_to_use_manual(self):
        from engine.agent.intent import SYSTEM_PROMPT

        assert '"manual"' in SYSTEM_PROMPT
        assert "Not every request has an event" in SYSTEM_PROMPT

    def test_the_planner_is_told_a_trigger_is_an_entry_point(self):
        from engine.agent.planner import SYSTEM_PROMPT

        assert "ENTRY POINT" in SYSTEM_PROMPT
        assert "Do not refuse a goal merely because" in SYSTEM_PROMPT


class TestHappyPath:
    def test_plans_and_compiles(self, session):
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        outcome = plan_workflow(session, "slack me on new issues", client)
        assert outcome.ok, outcome.issues
        assert outcome.compiled is not None
        assert outcome.compiled.execution_order == ["n1", "n2"]

    def test_accepts_on_the_first_attempt(self, session):
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        outcome = plan_workflow(session, "goal", client)
        assert len(outcome.attempts) == 1
        assert outcome.attempts[0].accepted

    def test_urls_come_from_the_database_not_the_model(self, session):
        # The model never emits a URL. This is the property that makes a
        # fabricated host impossible rather than merely unlikely.
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        outcome = plan_workflow(session, "goal", client)
        node = outcome.compiled.node("n2")
        assert node.base_url == "https://slack.com/api"
        assert node.method == "POST"

    def test_token_usage_is_accounted(self, session):
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        outcome = plan_workflow(session, "goal", client)
        assert outcome.usage.calls == 2


class TestHallucinationIsRefused:
    """The test the whole project exists to pass."""

    def test_invented_endpoint_id_is_rejected_before_the_database(self, session):
        # An id that never appeared in the candidate list is refused by the
        # allowlist, without a database lookup.
        fake = valid_plan(slack_id="ep_notreal123")
        client = StubClient([INTENT_RESPONSE, fake, fake, fake, fake])
        outcome = plan_workflow(session, "goal", client)
        assert not outcome.ok
        assert outcome.issues[0].code == "endpoint_not_in_candidates"
        assert outcome.issues[0].actual == "ep_notreal123"

    def test_a_real_endpoint_outside_the_candidate_list_is_still_rejected(self, session):
        # ep_gh_repo_delete exists in the database but was not offered for this
        # goal. Existing is not enough — it must have been retrieved.
        sneaky = valid_plan(slack_id="ep_gh_repo_delete")
        client = StubClient([INTENT_RESPONSE, sneaky, sneaky, sneaky, sneaky])
        outcome = plan_workflow(session, "goal", client)
        assert not outcome.ok
        assert all(i.code == "endpoint_not_in_candidates" for i in outcome.issues)

    def test_repeated_hallucination_stops_after_the_repair_budget(self, session):
        fake = valid_plan(slack_id="ep_notreal123")
        client = StubClient([INTENT_RESPONSE, fake, fake, fake, fake])
        outcome = plan_workflow(session, "goal", client, max_repairs=2)
        # One initial attempt plus two repairs, then it gives up rather than
        # looping forever on a model that will not comply.
        assert len(outcome.attempts) == 3
        assert not outcome.ok


class TestRefusal:
    """Answering "this cannot be done" is a feature, not a failure.

    The original prompt told the model to use the closest candidate when nothing
    fit. That produced workflows built from whatever happened to rank highest —
    structurally perfect, semantically nonsense, and far more misleading than an
    honest refusal.
    """

    def test_the_planner_can_report_that_nothing_fits(self, session):
        refusal = {
            "cannot_satisfy": {
                "reason": "no candidate reads Gmail or sends WhatsApp messages",
                "missing": ["gmail", "whatsapp"],
            }
        }
        client = StubClient([INTENT_RESPONSE, refusal])
        outcome = plan_workflow(session, "gmail to whatsapp", client)
        assert not outcome.ok
        assert outcome.issues[0].code == "no_suitable_endpoint"
        assert "gmail" in outcome.issues[0].actual

    def test_a_refusal_is_not_retried(self, session):
        # Re-running the same search would return the same candidates, so
        # retrying only spends tokens to reach the same conclusion.
        refusal = {"cannot_satisfy": {"reason": "nothing fits", "missing": ["x"]}}
        client = StubClient([INTENT_RESPONSE, refusal])
        outcome = plan_workflow(session, "goal", client, max_repairs=3)
        assert len(outcome.attempts) == 1

    def test_a_refusal_produces_no_workflow(self, session):
        refusal = {"cannot_satisfy": {"reason": "nothing fits", "missing": []}}
        client = StubClient([INTENT_RESPONSE, refusal])
        outcome = plan_workflow(session, "goal", client)
        assert outcome.compiled is None
        assert outcome.definition is None

    def test_the_prompt_offers_refusal_as_an_option(self, session):
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        plan_workflow(session, "goal", client)
        system = client.prompts[-1][0]
        assert "cannot_satisfy" in system
        assert "do NOT substitute the nearest match" in system


class TestRepairLoop:
    def test_a_rejected_plan_is_retried_with_the_validator_errors(self, session):
        broken = valid_plan(text_source="author")  # GitHub returns user.login
        client = StubClient([INTENT_RESPONSE, broken, valid_plan()])
        outcome = plan_workflow(session, "goal", client)
        assert outcome.ok
        assert len(outcome.attempts) == 2
        assert outcome.attempts[0].issues[0].code == Code.UNRESOLVABLE_SOURCE_PATH

    def test_the_repair_prompt_contains_the_structured_errors(self, session):
        broken = valid_plan(text_source="author")
        client = StubClient([INTENT_RESPONSE, broken, valid_plan()])
        plan_workflow(session, "goal", client)
        repair_prompt = client.prompts[-1][1]
        assert "VALIDATION ERRORS" in repair_prompt
        assert Code.UNRESOLVABLE_SOURCE_PATH in repair_prompt
        # The hint is what makes the next attempt likely to succeed.
        assert "response schema" in repair_prompt

    def test_a_malformed_plan_is_reported_and_retried(self, session):
        client = StubClient([INTENT_RESPONSE, {"nodes": "not a list"}, valid_plan()])
        outcome = plan_workflow(session, "goal", client)
        assert outcome.ok
        assert outcome.attempts[0].issues[0].code == "malformed_plan"

    def test_giving_up_returns_the_last_issues(self, session):
        broken = valid_plan(text_source="author")
        client = StubClient([INTENT_RESPONSE, broken, broken, broken, broken])
        outcome = plan_workflow(session, "goal", client, max_repairs=1)
        assert not outcome.ok
        assert outcome.issues
        assert outcome.compiled is None


class TestPromptConstruction:
    def test_candidates_are_listed_with_their_requirements(self, session):
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        plan_workflow(session, "goal", client)
        prompt = client.prompts[-1][1]
        assert "ep_slack_post" in prompt
        # Without being told what is mandatory the model cannot supply it, and
        # every plan would need a repair round.
        assert "REQUIRED" in prompt
        assert "RETURNS" in prompt

    def test_the_system_prompt_forbids_inventing_ids(self, session):
        client = StubClient([INTENT_RESPONSE, valid_plan()])
        plan_workflow(session, "goal", client)
        system = client.prompts[-1][0]
        assert "Never" in system and "invent" in system


class TestJsonExtraction:
    @pytest.mark.parametrize(
        "text",
        [
            '{"a": 1}',
            '```json\n{"a": 1}\n```',
            'Here you go:\n{"a": 1}',
            '```\n{"a": 1}\n```',
        ],
    )
    def test_json_is_recovered_from_common_wrappers(self, text):
        # Models add fences and preamble even when told not to.
        assert _extract_json(text) == {"a": 1}

    @pytest.mark.parametrize("text", ["no json here", "", "[1, 2, 3]"])
    def test_unusable_responses_raise(self, text):
        with pytest.raises(LLMError):
            _extract_json(text)
