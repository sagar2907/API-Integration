"""Execution tests.

Outbound HTTP is mocked with respx throughout — a test that reached a real
provider would be slow, flaky, and would send real messages. The properties
under test are the ones that are easy to claim and hard to prove: no duplicate
side effects, correct resumption, secrets never leaking, and a 200 response
that actually failed being treated as a failure.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from engine.db import Execution, ExecutionEvent, Job, NodeResult, Provider
from engine.execution import credentials as creds
from engine.execution.executor import (
    _body_reports_failure,
    build_request,
    execute_node,
)
from engine.execution.guards import BlockedRequest, check_url
from engine.execution.runner import (
    claim_job,
    create_execution,
    enqueue,
    finish_job,
    run_execution,
)
from engine.workflow.dag import CompiledInput, CompiledNode, CompiledWorkflow

FERNET_KEY = "8ZQmqNbW3aVYQ8kZ0nQ0Xk1u3z8dQ2xJ0hVQ9bZ7cQ4="


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    from engine.config import settings

    monkeypatch.setattr(settings, "credential_encryption_key", FERNET_KEY)
    monkeypatch.setattr(settings, "execution_allowlist_only", False)
    monkeypatch.setattr(settings, "http_max_retries", 2)
    monkeypatch.setattr(settings, "http_backoff_base_ms", 1)


def slack_node(node_id: str = "n2") -> CompiledNode:
    return CompiledNode(
        id=node_id,
        type="api_call",
        endpoint_id="ep_slack_post",
        provider_id="slack_com",
        method="POST",
        base_url="https://slack.com/api",
        path="/chat.postMessage",
        auth_scheme="bearer",
        inputs=[
            CompiledInput(location="body", name="channel", literal="#eng"),
            CompiledInput(location="body", name="text", source_node="n1", source_path="title"),
        ],
    )


def github_node() -> CompiledNode:
    return CompiledNode(
        id="n1",
        type="trigger",
        endpoint_id="ep_gh_issue_create",
        provider_id="github_com",
        method="POST",
        base_url="https://api.github.com",
        path="/repos/{owner}/{repo}/issues",
        inputs=[
            CompiledInput(location="path", name="owner", literal="acme"),
            CompiledInput(location="path", name="repo", literal="web"),
            CompiledInput(location="body", name="title", literal="Bug"),
        ],
    )


def workflow() -> CompiledWorkflow:
    return CompiledWorkflow(
        name="gh to slack", nodes=[github_node(), slack_node()], execution_order=["n1", "n2"]
    )


class TestRequestBuilding:
    def test_path_parameters_are_substituted(self):
        request = build_request(github_node(), {})
        assert request.url == "https://api.github.com/repos/acme/web/issues"

    def test_upstream_values_flow_into_the_body(self):
        request = build_request(slack_node(), {"n1": {"title": "Login broken"}})
        assert request.body == {"channel": "#eng", "text": "Login broken"}

    def test_nested_body_targets_build_nested_json(self):
        node = slack_node()
        node.inputs = [CompiledInput(location="body", name="message.text", literal="hi")]
        assert build_request(node, {}).body == {"message": {"text": "hi"}}

    def test_nested_source_paths_are_read(self):
        node = slack_node()
        node.inputs = [
            CompiledInput(location="body", name="text", source_node="n1", source_path="user.login")
        ]
        request = build_request(node, {"n1": {"user": {"login": "alex"}}})
        assert request.body == {"text": "alex"}

    def test_state_changing_calls_get_an_idempotency_key(self):
        assert build_request(
            slack_node(), {"n1": {"title": "x"}}, execution_id="ex1"
        ).idempotency_key

    def test_the_same_call_produces_the_same_key(self):
        # This is what makes a replay after a crash safe rather than duplicated.
        first = build_request(slack_node(), {"n1": {"title": "x"}}, execution_id="ex1")
        second = build_request(slack_node(), {"n1": {"title": "x"}}, execution_id="ex1")
        assert first.idempotency_key == second.idempotency_key

    def test_different_executions_produce_different_keys(self):
        first = build_request(slack_node(), {"n1": {"title": "x"}}, execution_id="ex1")
        second = build_request(slack_node(), {"n1": {"title": "x"}}, execution_id="ex2")
        assert first.idempotency_key != second.idempotency_key

    def test_read_only_calls_get_no_key(self):
        node = github_node()
        node.method = "GET"
        assert build_request(node, {}).idempotency_key is None

    def test_a_summary_never_contains_header_values_or_the_body(self):
        request = build_request(slack_node(), {"n1": {"title": "secret text"}})
        request.headers["Authorization"] = "Bearer super-secret"
        described = str(request.describe())
        assert "super-secret" not in described
        assert "secret text" not in described


class TestPathSyntaxMatchesTheValidator:
    """The validator and the executor must agree on what a path means.

    They are separate implementations — one walks a JSON Schema, the other walks
    a real response — so nothing but a test keeps them aligned. If the compiler
    approves `messages[0].id` and the executor cannot follow it, a workflow
    passes every check and then fails at runtime, which is exactly the failure
    this project claims to prevent.
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("messages[0].id", "m1"),
            ("messages[].id", "m1"),
            ("messages[1].id", "m2"),
            ("user.login", "alex"),
            ("labels[].name", "bug"),
            ("count", 2),
        ],
    )
    def test_the_executor_reads_every_accepted_shape(self, path, expected):
        from engine.execution.executor import _read_path

        data = {
            "messages": [{"id": "m1"}, {"id": "m2"}],
            "user": {"login": "alex"},
            "labels": [{"name": "bug"}],
            "count": 2,
        }
        assert _read_path(data, path) == expected

    @pytest.mark.parametrize("path", ["messages[9].id", "missing", "user.email", "count.x"])
    def test_paths_that_do_not_exist_return_nothing(self, path):
        from engine.execution.executor import _read_path

        data = {"messages": [{"id": "m1"}], "user": {"login": "alex"}, "count": 2}
        assert _read_path(data, path) is None

    def test_both_sides_use_the_same_array_pattern(self):
        # Sharing the literal pattern is the only structural guarantee that the
        # two resolvers cannot drift apart.
        from engine.execution.executor import _ARRAY_SUFFIX as executor_pattern
        from engine.workflow.schema_match import _ARRAY_SUFFIX as compiler_pattern

        assert executor_pattern.pattern == compiler_pattern.pattern

    def test_a_path_the_compiler_accepts_is_readable_at_runtime(self):
        from engine.execution.executor import _read_path
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
        response = {"messages": [{"id": "m1"}]}
        for path in ("messages[0].id", "messages[].id"):
            assert resolve_path(schema, path) is not None
            assert _read_path(response, path) == "m1"


class TestHiddenFailures:
    @pytest.mark.parametrize(
        "payload",
        [
            {"ok": False, "error": "channel_not_found"},
            {"success": False, "message": "nope"},
            {"status": "error", "message": "bad"},
        ],
    )
    def test_a_200_that_actually_failed_is_detected(self, payload):
        assert _body_reports_failure(payload) is not None

    @pytest.mark.parametrize("payload", [{"ok": True}, {"id": 1}, {}, [1, 2], "text"])
    def test_genuine_successes_pass(self, payload):
        assert _body_reports_failure(payload) is None

    @respx.mock
    def test_slack_style_failure_is_not_reported_as_success(self, session):
        # Slack answers 200 OK while doing nothing. Trusting the status code
        # means the automation lies about having worked.
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb-token")
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="ex1")
        assert not outcome.ok
        assert outcome.status_code == 200
        assert outcome.error_category == "provider"
        assert "channel_not_found" in outcome.error_message


class TestRetries:
    @respx.mock
    def test_a_server_error_is_retried_then_succeeds(self, session):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            side_effect=[
                httpx.Response(503, text="unavailable"),
                httpx.Response(200, json={"ok": True, "ts": "1"}),
            ]
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert outcome.ok
        assert outcome.attempts == 2
        assert route.call_count == 2

    @respx.mock
    def test_rate_limits_are_retried(self, session):
        respx.post("https://slack.com/api/chat.postMessage").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "0"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        assert execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e").ok

    @respx.mock
    def test_an_auth_failure_is_not_retried(self, session):
        # Retrying a bad credential just burns quota; it will never succeed.
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(401, text="invalid_auth")
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == "auth"
        assert route.call_count == 1

    @pytest.mark.parametrize(
        ("status", "category", "retried"),
        [
            (500, "provider", True),
            (502, "provider", True),
            (503, "provider", True),
            (429, "rate_limit", True),
            (400, "validation", False),
            (404, "validation", False),
            (422, "validation", False),
            (401, "auth", False),
            (403, "auth", False),
        ],
    )
    @respx.mock
    def test_only_recoverable_failures_are_retried(self, session, status, category, retried):
        """A 4xx means the request itself is wrong.

        Retrying it cannot help: it burns quota and delays the error the user
        needs to see. Only server-side and transport failures get another go.
        """
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(status, text="nope")
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == category
        assert (route.call_count > 1) is retried

    @respx.mock
    def test_timeouts_are_retried_then_give_up(self, session):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            side_effect=httpx.ConnectTimeout("too slow")
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == "network"
        assert route.call_count == 3  # initial attempt plus two retries


class TestCredentials:
    def test_a_stored_secret_is_encrypted_at_rest(self, session):
        row = creds.store_credential(session, provider_id="slack_com", secret="xoxb-hunter2")
        assert b"xoxb-hunter2" not in row.ciphertext

    def test_a_secret_round_trips(self, session):
        creds.store_credential(session, provider_id="slack_com", secret="xoxb-hunter2")
        assert creds.resolve_secret(session, "slack_com") == "xoxb-hunter2"

    def test_a_missing_credential_names_the_provider(self, session):
        with pytest.raises(creds.CredentialError, match="slack_com"):
            creds.resolve_secret(session, "slack_com")

    def test_every_use_is_audited(self, session):
        from engine.db import CredentialAudit

        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        creds.resolve_secret(session, "slack_com", execution_id="ex1", node_id="n2")
        session.commit()
        audit = session.query(CredentialAudit).all()
        assert len(audit) == 1
        assert audit[0].execution_id == "ex1"

    def test_bearer_and_oauth_tokens_are_sent_the_same_way(self):
        # The OAuth *flow* is out of scope; a token already held is not.
        for scheme in ("bearer", "oauth2"):
            headers: dict[str, str] = {}
            creds.apply_auth(headers, {}, scheme=scheme, secret="tok")
            assert headers["Authorization"] == "Bearer tok"

    def test_an_api_key_can_go_in_the_query_string(self):
        params: dict[str, str] = {}
        creds.apply_auth(
            {}, params, scheme="apikey", secret="k", location="query", parameter_name="api_key"
        )
        assert params == {"api_key": "k"}

    @respx.mock
    def test_a_secret_never_appears_in_an_error_message(self, session):
        # Providers do echo credentials back in error bodies.
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(400, text="bad token xoxb-hunter2 rejected")
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb-hunter2")
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert "xoxb-hunter2" not in (outcome.error_message or "")
        assert "<REDACTED>" in outcome.error_message


class TestEgressGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8080/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/",
            "file:///etc/passwd",
            "ftp://example.com/",
        ],
    )
    def test_internal_and_non_http_targets_are_blocked(self, url):
        with pytest.raises(BlockedRequest):
            check_url(url)

    def test_a_public_https_url_passes(self):
        check_url("https://slack.com/api/chat.postMessage")

    def test_the_allowlist_is_enforced_when_enabled(self, monkeypatch):
        from engine.config import settings

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        with pytest.raises(BlockedRequest, match="allowlist"):
            check_url("https://stranger.example.com/x", provider_id="stranger_com")

    @respx.mock
    def test_a_blocked_url_is_never_dispatched(self, session):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        node = slack_node()
        node.base_url = "http://169.254.169.254"
        outcome = execute_node(session, node, {"n1": {"title": "x"}}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == "policy"
        assert route.call_count == 0


class TestAllowlistSource:
    """A provider may be permitted from the environment or from the interface.

    The database flag existed from day one but was written and never read, so
    the only way to permit a provider was to edit a file and restart. That is a
    poor trade for a catch that exists to stop *accidental* calls.
    """

    def test_the_environment_permits_a_provider(self, session, monkeypatch):
        from engine.config import settings
        from engine.execution.policy import is_allowed

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        monkeypatch.setattr(settings, "execution_allowlist", "slack.com")
        assert is_allowed(session, "slack_com") is True
        assert is_allowed(session, "stranger_com") is False

    def test_the_database_flag_also_permits_a_provider(self, session, monkeypatch):
        from engine.config import settings
        from engine.execution.policy import is_allowed, set_allowed

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        monkeypatch.setattr(settings, "execution_allowlist", "")
        assert is_allowed(session, "stranger_com") is False
        set_allowed(session, "stranger_com", True)
        assert is_allowed(session, "stranger_com") is True

    def test_permission_can_be_withdrawn(self, session, monkeypatch):
        from engine.config import settings
        from engine.execution.policy import is_allowed, set_allowed

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        monkeypatch.setattr(settings, "execution_allowlist", "")
        set_allowed(session, "stranger_com", True)
        set_allowed(session, "stranger_com", False)
        assert is_allowed(session, "stranger_com") is False

    def test_disabling_the_catch_permits_everything(self, session, monkeypatch):
        from engine.config import settings
        from engine.execution.policy import is_allowed

        monkeypatch.setattr(settings, "execution_allowlist_only", False)
        assert is_allowed(session, "anything_at_all") is True

    def test_an_unknown_provider_cannot_be_permitted(self, session):
        from engine.execution.policy import set_allowed

        with pytest.raises(LookupError):
            set_allowed(session, "does_not_exist", True)

    @respx.mock
    def test_enabling_a_provider_lets_a_real_call_through(self, session, monkeypatch):
        from engine.config import settings
        from engine.execution.policy import set_allowed

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        monkeypatch.setattr(settings, "execution_allowlist", "")
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")

        blocked = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert blocked.error_category == "policy"
        assert route.call_count == 0

        set_allowed(session, "slack_com", True)
        session.commit()
        allowed = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert allowed.ok
        assert route.call_count == 1


class TestDryRun:
    @respx.mock
    def test_the_request_is_built_but_never_sent(self, session):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(
            session, slack_node(), {"n1": {"title": "x"}}, execution_id="e", dry_run=True
        )
        assert outcome.ok
        assert outcome.dry_run
        assert route.call_count == 0
        assert outcome.request.url == "https://slack.com/api/chat.postMessage"

    @respx.mock
    def test_a_later_node_can_be_dry_run_without_upstream_data(self, session):
        # Nothing really ran upstream, so the value node 2 needs does not exist.
        # A dry run must still prove the request can be built and checked.
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(session, slack_node(), {}, execution_id="e", dry_run=True)
        assert outcome.ok
        assert route.call_count == 0
        assert "text" in outcome.request.body
        assert "dry-run" in outcome.request.body["text"]

    @respx.mock
    def test_a_real_run_still_fails_on_a_missing_upstream_value(self, session):
        # The placeholder is a dry-run affordance only. A real execution with a
        # missing value is a data-mapping failure and must not be papered over.
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        outcome = execute_node(session, slack_node(), {}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == "data_mapping"

    @respx.mock
    def test_a_dry_run_works_without_a_configured_credential(self, session):
        # Previewing a workflow must not require every provider secret to be
        # configured first — that would make the feature useless for exactly
        # the case it exists for.
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        outcome = execute_node(
            session, slack_node(), {"n1": {"title": "x"}}, execution_id="e", dry_run=True
        )
        assert outcome.ok
        assert outcome.output["credential_missing"] is True
        assert route.call_count == 0

    @respx.mock
    def test_a_real_run_still_requires_a_credential(self, session):
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == "auth"

    @respx.mock
    def test_a_dry_run_reports_a_policy_block_instead_of_failing(self, session, monkeypatch):
        """The allowlist governs sending, not building.

        Failing the preview would hide a correctly-built request behind a policy
        message, and make previewing impossible for any provider not yet
        enabled — which is exactly when you most want to preview.
        """
        from engine.config import settings

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        monkeypatch.setattr(settings, "execution_allowlist", "github.com")
        # slack_com must be off in the database as well, now that a provider
        # can be permitted from the interface rather than only the environment.
        session.get(Provider, "slack_com").allowlisted = False
        session.commit()
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        outcome = execute_node(
            session, slack_node(), {"n1": {"title": "x"}}, execution_id="e", dry_run=True
        )
        assert outcome.ok
        assert "allowlist" in outcome.output["would_be_blocked"]
        assert route.call_count == 0
        # The request itself was still built properly.
        assert outcome.request.url == "https://slack.com/api/chat.postMessage"

    @respx.mock
    def test_a_real_run_still_refuses_a_provider_off_the_allowlist(self, session, monkeypatch):
        from engine.config import settings

        monkeypatch.setattr(settings, "execution_allowlist_only", True)
        monkeypatch.setattr(settings, "execution_allowlist", "github.com")
        # slack_com must be off in the database as well, now that a provider
        # can be permitted from the interface rather than only the environment.
        session.get(Provider, "slack_com").allowlisted = False
        session.commit()
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        outcome = execute_node(session, slack_node(), {"n1": {"title": "x"}}, execution_id="e")
        assert not outcome.ok
        assert outcome.error_category == "policy"
        assert route.call_count == 0

    @respx.mock
    def test_a_dry_run_still_reports_an_ssrf_target(self, session):
        # Private addresses are reported too — a preview must never imply that
        # a request to internal infrastructure would go through.
        node = slack_node()
        node.base_url = "http://169.254.169.254"
        outcome = execute_node(
            session, node, {"n1": {"title": "x"}}, execution_id="e", dry_run=True
        )
        assert outcome.ok
        assert "private address" in outcome.output["would_be_blocked"]


class TestExecutionAndResumption:
    @respx.mock
    def test_a_whole_workflow_runs(self, session):
        respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"number": 7, "title": "Login broken"})
        )
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "ts": "1"})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        execution = create_execution(session, workflow_id="wf1")
        run_execution(session, execution, workflow())
        assert execution.status == "succeeded"

    @respx.mock
    def test_the_second_node_receives_the_first_nodes_output(self, session):
        respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"title": "Login broken"})
        )
        slack = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        run_execution(session, create_execution(session, workflow_id="wf1"), workflow())
        import json as _json

        sent = _json.loads(slack.calls[0].request.content)
        assert sent["text"] == "Login broken"

    @respx.mock
    def test_a_failure_stops_the_run_and_records_the_category(self, session):
        respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(500, text="boom")
        )
        slack = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        execution = create_execution(session, workflow_id="wf1")
        run_execution(session, execution, workflow())
        assert execution.status == "failed"
        assert execution.error_category == "provider"
        # The downstream node must not run after an upstream failure.
        assert slack.call_count == 0

    @respx.mock
    def test_a_completed_node_is_not_run_again_on_resume(self, session):
        """The crash test: the side effect must happen exactly once.

        The first run sends the Slack message, then the process 'dies' before
        the job is acknowledged. The job is redelivered and the execution runs
        again — and Slack must not receive a second message.
        """
        github = respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"title": "Login broken"})
        )
        slack = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "ts": "1"})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")

        execution = create_execution(session, workflow_id="wf1")
        run_execution(session, execution, workflow())
        assert execution.status == "succeeded"
        assert github.call_count == 1
        assert slack.call_count == 1

        # Redelivery of the same execution after a crash.
        run_execution(session, execution, workflow())

        assert github.call_count == 1, "GitHub was called twice after resume"
        assert slack.call_count == 1, "Slack received a duplicate message"

    @respx.mock
    def test_resume_continues_from_the_checkpoint(self, session):
        """A crash between two nodes replays only the unfinished one."""
        github = respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"title": "Login broken"})
        )
        slack = respx.post("https://slack.com/api/chat.postMessage").mock(
            side_effect=[
                httpx.ConnectTimeout("worker died here"),
                httpx.ConnectTimeout("worker died here"),
                httpx.ConnectTimeout("worker died here"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")

        execution = create_execution(session, workflow_id="wf1")
        run_execution(session, execution, workflow())
        assert execution.status == "failed"
        assert github.call_count == 1

        # The failed node is cleared for retry; the completed one is not.
        session.query(NodeResult).filter(
            NodeResult.execution_id == execution.execution_id,
            NodeResult.status == "failed",
        ).delete()
        session.commit()

        run_execution(session, execution, workflow())
        assert execution.status == "succeeded"
        # GitHub was never called again: its checkpoint was honoured.
        assert github.call_count == 1
        assert slack.call_count == 4

    @respx.mock
    def test_every_step_is_recorded(self, session):
        respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"title": "x"})
        )
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        execution = create_execution(session, workflow_id="wf1")
        run_execution(session, execution, workflow())
        events = session.query(ExecutionEvent).all()
        types = {e.event_type for e in events}
        assert "execution_started" in types
        assert "node_succeeded" in types
        assert "execution_succeeded" in types

    @respx.mock
    def test_logged_events_never_contain_payload_bodies(self, session):
        respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"title": "confidential subject"})
        )
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")
        execution = create_execution(session, workflow_id="wf1")
        run_execution(session, execution, workflow())
        recorded = str([e.payload_metadata for e in session.query(ExecutionEvent).all()])
        assert "confidential subject" not in recorded


class TestJobQueue:
    def test_a_queued_job_can_be_claimed(self, session):
        execution = create_execution(session, workflow_id="wf1")
        enqueue(session, execution)
        session.commit()
        job = claim_job(session, worker_id="w1")
        assert job is not None
        assert job.status == "running"
        assert job.claimed_by == "w1"

    def test_a_claimed_job_is_not_handed_to_a_second_worker(self, session):
        execution = create_execution(session, workflow_id="wf1")
        enqueue(session, execution)
        session.commit()
        assert claim_job(session, worker_id="w1") is not None
        # The lease is what stops two workers running the same execution.
        assert claim_job(session, worker_id="w2") is None

    def test_an_expired_lease_is_reclaimed(self, session):
        from datetime import timedelta

        from engine.db import naive_utcnow

        execution = create_execution(session, workflow_id="wf1")
        job = enqueue(session, execution)
        session.commit()
        claim_job(session, worker_id="w1")
        # Simulate the first worker dying without renewing its lease.
        job.lease_until = naive_utcnow() - timedelta(seconds=1)
        session.commit()
        reclaimed = claim_job(session, worker_id="w2")
        assert reclaimed is not None
        assert reclaimed.claimed_by == "w2"
        assert reclaimed.deliveries == 2

    def test_an_empty_queue_returns_nothing(self, session):
        assert claim_job(session, worker_id="w1") is None

    def test_a_successful_run_acknowledges_the_job(self, session):
        execution = create_execution(session, workflow_id="wf1")
        job = enqueue(session, execution)
        session.commit()
        execution.status = "succeeded"
        finish_job(session, job, execution)
        assert job.status == "done"

    def test_repeated_failure_reaches_the_dead_letter_queue(self, session, monkeypatch):
        from engine.config import settings

        monkeypatch.setattr(settings, "queue_max_deliveries", 2)
        execution = create_execution(session, workflow_id="wf1")
        job = enqueue(session, execution)
        session.commit()
        execution.status = "failed"
        execution.error_message = "kept failing"

        job.deliveries = 1
        finish_job(session, job, execution)
        assert job.status == "queued"  # still worth another attempt

        job.deliveries = 2
        finish_job(session, job, execution)
        assert job.status == "dead_letter"  # a human looks at it now


class TestWorkerLoop:
    @respx.mock
    def test_the_worker_runs_a_queued_execution(self, session, monkeypatch):
        import engine.execution.worker as worker_module
        from engine.db import CompiledIR

        respx.post("https://api.github.com/repos/acme/web/issues").mock(
            return_value=httpx.Response(201, json={"title": "x"})
        )
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        creds.store_credential(session, provider_id="slack_com", secret="xoxb")

        session.add(
            CompiledIR(
                ir_id="ir1",
                workflow_id="wf1",
                workflow_version=1,
                ir=workflow().model_dump(mode="json"),
            )
        )
        execution = create_execution(session, workflow_id="wf1")
        enqueue(session, execution)
        session.commit()

        from contextlib import contextmanager

        @contextmanager
        def fake_scope():
            yield session

        monkeypatch.setattr(worker_module, "session_scope", fake_scope)
        assert worker_module.run_once(worker="w1") is True
        assert session.get(Execution, execution.execution_id).status == "succeeded"

    def test_the_worker_reports_an_empty_queue(self, session, monkeypatch):
        from contextlib import contextmanager

        import engine.execution.worker as worker_module

        @contextmanager
        def fake_scope():
            yield session

        monkeypatch.setattr(worker_module, "session_scope", fake_scope)
        assert worker_module.run_once(worker="w1") is False

    def test_an_execution_without_a_compiled_form_fails_cleanly(self, session, monkeypatch):
        from contextlib import contextmanager

        import engine.execution.worker as worker_module

        execution = create_execution(session, workflow_id="wf-missing")
        enqueue(session, execution)
        session.commit()

        @contextmanager
        def fake_scope():
            yield session

        monkeypatch.setattr(worker_module, "session_scope", fake_scope)
        worker_module.run_once(worker="w1")
        assert session.get(Execution, execution.execution_id).status == "failed"
        assert session.query(Job).first().status in ("queued", "dead_letter")
