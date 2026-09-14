"""Compiler tests — the gate the whole project depends on.

Each broken plan below asserts a *specific* rejection code, not merely that
compilation failed. A validator that rejects everything would pass a weaker
test suite while being useless; these pin down that the right check fired for
the right reason.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from engine.workflow.compiler import compile_workflow
from engine.workflow.dag import Code, Edge, NodeInput, WorkflowDefinition, WorkflowNode


def literal(target: str, value: object) -> NodeInput:
    return NodeInput(target=target, literal=value)


def from_node(target: str, node: str, path: str) -> NodeInput:
    return NodeInput(target=target, source_node=node, source_path=path)


def github_to_slack(**overrides) -> WorkflowDefinition:
    """The canonical valid workflow: GitHub issue in, Slack message out."""
    trigger = WorkflowNode(
        id="n1",
        type="trigger",
        endpoint_id="ep_gh_issue_create",
        name="New GitHub issue",
        inputs=[
            literal("path.owner", "acme"),
            literal("path.repo", "web"),
            literal("body.title", "placeholder"),
        ],
    )
    action = WorkflowNode(
        id="n2",
        type="api_call",
        endpoint_id="ep_slack_post",
        name="Post to Slack",
        inputs=[
            literal("header.token", "xoxb-test"),
            literal("body.channel", "#general"),
            from_node("body.text", "n1", "title"),
        ],
    )
    definition = WorkflowDefinition(
        name="github issue to slack",
        nodes=[trigger, action],
        edges=[Edge(source="n1", target="n2")],
    )
    for key, value in overrides.items():
        setattr(definition, key, value)
    return definition


class TestValidWorkflow:
    def test_compiles(self, session):
        result = compile_workflow(session, github_to_slack())
        assert result.ok, result.issues
        assert result.workflow is not None

    def test_execution_order_follows_dependencies(self, session):
        result = compile_workflow(session, github_to_slack())
        assert result.workflow.execution_order == ["n1", "n2"]

    def test_request_details_come_from_the_database_not_the_plan(self, session):
        # The plan never states a URL or a method. Both are filled in from the
        # endpoint record, which is what makes a fabricated host impossible.
        node = compile_workflow(session, github_to_slack()).workflow.node("n2")
        assert node.method == "POST"
        assert node.base_url == "https://slack.com/api"
        assert node.path == "/chat.postMessage"
        assert node.auth_scheme == "bearer"

    def test_inputs_are_compiled_with_their_locations(self, session):
        node = compile_workflow(session, github_to_slack()).workflow.node("n2")
        by_name = {(i.location, i.name): i for i in node.inputs}
        assert by_name[("header", "token")].literal == "xoxb-test"
        assert by_name[("body", "text")].source_node == "n1"
        assert by_name[("body", "text")].source_path == "title"


class TestRejections:
    """Six deliberately broken plans, each rejected for a specific reason."""

    def test_1_fabricated_endpoint_id_is_rejected(self, session):
        # The single most important test in the project: an endpoint the model
        # invented resolves to nothing and must never execute.
        definition = github_to_slack()
        definition.nodes[1].endpoint_id = "ep_deadbeef1234"
        result = compile_workflow(session, definition)
        assert not result.ok
        assert Code.ENDPOINT_NOT_FOUND in result.codes
        issue = next(i for i in result.issues if i.code == Code.ENDPOINT_NOT_FOUND)
        assert issue.actual == "ep_deadbeef1234"
        assert issue.node_id == "n2"

    def test_2_missing_required_body_field_is_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[1].inputs = [
            i for i in definition.nodes[1].inputs if i.target != "body.channel"
        ]
        result = compile_workflow(session, definition)
        assert not result.ok
        issue = next(i for i in result.issues if i.code == Code.MISSING_REQUIRED_BODY_FIELD)
        assert issue.field == "body.channel"

    def test_3_missing_required_path_parameter_is_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[0].inputs = [
            i for i in definition.nodes[0].inputs if i.target != "path.owner"
        ]
        result = compile_workflow(session, definition)
        assert not result.ok
        issue = next(i for i in result.issues if i.code == Code.MISSING_REQUIRED_PARAMETER)
        assert issue.field == "path.owner"

    def test_4_type_mismatch_between_nodes_is_rejected(self, session):
        # `user` is an object; Slack's `text` is a string. Nothing can coerce
        # an object into a string safely, so this must not run.
        definition = github_to_slack()
        definition.nodes[1].inputs = [
            i for i in definition.nodes[1].inputs if i.target != "body.text"
        ] + [from_node("body.text", "n1", "user")]
        result = compile_workflow(session, definition)
        assert not result.ok
        issue = next(i for i in result.issues if i.code == Code.SCHEMA_INCOMPATIBLE)
        assert issue.actual == "object"
        assert issue.expected == "string"

    def test_5_unsupported_auth_is_rejected(self, session):
        definition = WorkflowDefinition(
            name="oauth only",
            nodes=[
                WorkflowNode(
                    id="n1",
                    type="trigger",
                    endpoint_id="ep_oauth_thing",
                    inputs=[literal("body.name", "x")],
                )
            ],
        )
        result = compile_workflow(session, definition)
        assert not result.ok
        issue = next(i for i in result.issues if i.code == Code.UNSUPPORTED_AUTH)
        assert issue.actual == "oauth2"

    def test_6_cycle_is_rejected(self, session):
        definition = github_to_slack()
        definition.edges = [Edge(source="n1", target="n2"), Edge(source="n2", target="n1")]
        result = compile_workflow(session, definition)
        assert not result.ok
        assert Code.CYCLE_DETECTED in result.codes


class TestMoreRejections:
    def test_method_claimed_by_the_plan_must_match_the_record(self, session):
        definition = github_to_slack()
        definition.nodes[1].config = {"method": "GET"}
        result = compile_workflow(session, definition)
        issue = next(i for i in result.issues if i.code == Code.METHOD_MISMATCH)
        assert issue.expected == "POST"
        assert issue.actual == "GET"

    def test_path_claimed_by_the_plan_must_match_the_record(self, session):
        definition = github_to_slack()
        definition.nodes[1].config = {"path": "/chat.sendMessage"}
        result = compile_workflow(session, definition)
        assert Code.PATH_MISMATCH in result.codes

    def test_destructive_call_without_approval_is_rejected(self, session):
        definition = WorkflowDefinition(
            name="delete a repo",
            nodes=[
                WorkflowNode(
                    id="n1",
                    type="trigger",
                    endpoint_id="ep_gh_issue_create",
                    inputs=[
                        literal("path.owner", "acme"),
                        literal("path.repo", "web"),
                        literal("body.title", "x"),
                    ],
                ),
                WorkflowNode(
                    id="n2",
                    type="api_call",
                    endpoint_id="ep_gh_repo_delete",
                    inputs=[literal("path.owner", "acme"), literal("path.repo", "web")],
                ),
            ],
            edges=[Edge(source="n1", target="n2")],
        )
        result = compile_workflow(session, definition)
        assert Code.DESTRUCTIVE_REQUIRES_APPROVAL in result.codes

    def test_destructive_call_with_upstream_approval_passes(self, session):
        definition = WorkflowDefinition(
            name="delete a repo, with approval",
            nodes=[
                WorkflowNode(
                    id="n1",
                    type="trigger",
                    endpoint_id="ep_gh_issue_create",
                    inputs=[
                        literal("path.owner", "acme"),
                        literal("path.repo", "web"),
                        literal("body.title", "x"),
                    ],
                ),
                WorkflowNode(id="n2", type="approval", name="Confirm deletion"),
                WorkflowNode(
                    id="n3",
                    type="api_call",
                    endpoint_id="ep_gh_repo_delete",
                    inputs=[literal("path.owner", "acme"), literal("path.repo", "web")],
                ),
            ],
            edges=[Edge(source="n1", target="n2"), Edge(source="n2", target="n3")],
        )
        result = compile_workflow(session, definition)
        assert result.ok, result.issues

    def test_deprecated_endpoint_is_rejected(self, session):
        definition = WorkflowDefinition(
            name="legacy",
            nodes=[
                WorkflowNode(
                    id="n1",
                    type="trigger",
                    endpoint_id="ep_gh_legacy",
                    inputs=[literal("path.team_id", "42")],
                )
            ],
        )
        assert Code.ENDPOINT_DEPRECATED in compile_workflow(session, definition).codes

    def test_provider_outside_the_allowlist_is_rejected(self, session):
        definition = WorkflowDefinition(
            name="stranger",
            nodes=[
                WorkflowNode(id="n1", type="trigger", endpoint_id="ep_stranger_thing"),
            ],
        )
        result = compile_workflow(session, definition, enforce_allowlist=True)
        assert Code.PROVIDER_NOT_ALLOWLISTED in result.codes

    def test_allowlist_can_be_disabled_for_planning(self, session):
        # Discovery and planning span every indexed provider; only execution is
        # restricted. Compiling with the check off must not report it.
        definition = WorkflowDefinition(
            name="stranger",
            nodes=[WorkflowNode(id="n1", type="trigger", endpoint_id="ep_stranger_thing")],
        )
        result = compile_workflow(session, definition, enforce_allowlist=False)
        assert Code.PROVIDER_NOT_ALLOWLISTED not in result.codes

    def test_source_path_the_endpoint_never_returns_is_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[1].inputs = [
            i for i in definition.nodes[1].inputs if i.target != "body.text"
        ] + [from_node("body.text", "n1", "author")]
        result = compile_workflow(session, definition)
        issue = next(i for i in result.issues if i.code == Code.UNRESOLVABLE_SOURCE_PATH)
        # GitHub returns user.login, never author — the classic mistake.
        assert "author" in issue.actual

    def test_nested_source_path_resolves(self, session):
        definition = github_to_slack()
        definition.nodes[1].inputs = [
            i for i in definition.nodes[1].inputs if i.target != "body.text"
        ] + [from_node("body.text", "n1", "user.login")]
        assert compile_workflow(session, definition).ok

    def test_unknown_path_parameter_is_reported(self, session):
        definition = github_to_slack()
        definition.nodes[0].inputs.append(literal("path.organisation", "acme"))
        assert Code.UNKNOWN_PARAMETER in compile_workflow(session, definition).codes

    def test_workflow_without_a_trigger_is_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[0].type = "api_call"
        assert Code.NO_TRIGGER in compile_workflow(session, definition).codes

    def test_two_triggers_are_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[1].type = "trigger"
        assert Code.MULTIPLE_TRIGGERS in compile_workflow(session, definition).codes

    def test_unreachable_node_is_rejected(self, session):
        definition = github_to_slack()
        definition.edges = []
        assert Code.UNREACHABLE_NODE in compile_workflow(session, definition).codes

    def test_edge_to_a_nonexistent_node_is_rejected(self, session):
        definition = github_to_slack()
        definition.edges.append(Edge(source="n2", target="n99"))
        assert Code.UNKNOWN_NODE_REFERENCE in compile_workflow(session, definition).codes

    def test_endpoint_node_without_an_endpoint_id_is_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[1].endpoint_id = None
        assert Code.MISSING_ENDPOINT_ID in compile_workflow(session, definition).codes

    def test_input_with_an_unknown_location_is_rejected(self, session):
        definition = github_to_slack()
        definition.nodes[1].inputs.append(literal("cookie.session", "abc"))
        assert Code.INVALID_TARGET in compile_workflow(session, definition).codes


class TestPlanShapeIsEnforcedBeforeCompilation:
    """Malformed plans fail at parse time, before any database work."""

    def test_unknown_field_is_refused(self):
        # extra="forbid" is what stops invented structure entering the pipeline.
        with pytest.raises(PydanticValidationError):
            WorkflowNode(id="n1", type="api_call", invented_field="surprise")

    def test_unknown_node_type_is_refused(self):
        with pytest.raises(PydanticValidationError):
            WorkflowNode(id="n1", type="teleport")

    def test_input_needs_exactly_one_source(self):
        with pytest.raises(PydanticValidationError):
            NodeInput(target="body.text", source_node="n1", source_path="title", literal="x")
        with pytest.raises(PydanticValidationError):
            NodeInput(target="body.text")

    def test_input_from_a_node_needs_a_path(self):
        with pytest.raises(PydanticValidationError):
            NodeInput(target="body.text", source_node="n1")


class TestIssuesAreMachineReadable:
    def test_every_issue_carries_a_code_and_message(self, session):
        definition = github_to_slack()
        definition.nodes[1].endpoint_id = "ep_nope"
        for issue in compile_workflow(session, definition).issues:
            assert issue.code
            assert issue.message

    def test_failure_carries_no_compiled_workflow(self, session):
        definition = github_to_slack()
        definition.nodes[1].endpoint_id = "ep_nope"
        result = compile_workflow(session, definition)
        # There is no partial success: a rejected plan yields nothing runnable.
        assert result.workflow is None
        assert not result.ok
