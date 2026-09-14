"""Parser tests.

Real specs are hostile: circular references, Swagger 2.0 mixed with OpenAPI 3,
missing blocks. These tests pin the behaviour that ingestion depends on, above all
that a malformed document raises SpecError rather than escaping as some arbitrary
exception that would abort a whole ingestion run.
"""

from __future__ import annotations

import pytest

from engine.ingest.parse import SpecError, parse_spec, resolve_refs

OPENAPI_3 = {
    "openapi": "3.0.0",
    "info": {"title": "Demo API", "version": "2.1", "x-providerName": "demo.com"},
    "servers": [{"url": "https://api.demo.com/v2"}],
    "components": {
        "schemas": {
            "Message": {
                "type": "object",
                "required": ["channel"],
                "properties": {
                    "channel": {"type": "string"},
                    "text": {"type": "string"},
                },
            }
        },
        "securitySchemes": {
            "apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "oauth": {
                "type": "oauth2",
                "flows": {"authorizationCode": {"scopes": {"chat:write": "post messages"}}},
            },
        },
    },
    "paths": {
        "/channels/{channel_id}/messages": {
            "parameters": [
                {"name": "channel_id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "post": {
                "operationId": "messages/create",
                "summary": "Post a message",
                "tags": ["messages"],
                "parameters": [{"name": "dry_run", "in": "query", "schema": {"type": "boolean"}}],
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Message"}}
                    }
                },
                "responses": {
                    "201": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                }
                            }
                        }
                    }
                },
            },
            "delete": {"operationId": "messages/delete", "responses": {"204": {}}},
        }
    },
}

SWAGGER_2 = {
    "swagger": "2.0",
    "info": {"title": "Legacy API", "version": "1.0"},
    "host": "legacy.example.com",
    "basePath": "/api",
    "schemes": ["https"],
    "definitions": {
        "Payload": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        }
    },
    "securityDefinitions": {"basicAuth": {"type": "basic"}},
    "paths": {
        "/things": {
            "post": {
                "operationId": "createThing",
                "parameters": [
                    {"name": "body", "in": "body", "schema": {"$ref": "#/definitions/Payload"}},
                    {"name": "verbose", "in": "query", "type": "boolean"},
                ],
                "responses": {
                    "200": {"schema": {"type": "object", "properties": {"id": {"type": "string"}}}}
                },
            }
        }
    },
}


def _endpoint(api, method, path):
    return next(e for e in api.endpoints if e.method == method and e.path == path)


class TestOpenApi3:
    def test_extracts_api_metadata(self):
        api = parse_spec(OPENAPI_3)
        assert api.provider_name == "demo.com"
        assert api.title == "Demo API"
        assert api.version == "2.1"
        assert api.base_url == "https://api.demo.com/v2"

    def test_merges_path_level_and_operation_level_parameters(self):
        endpoint = _endpoint(parse_spec(OPENAPI_3), "POST", "/channels/{channel_id}/messages")
        by_name = {p.name: p for p in endpoint.parameters}
        assert by_name["channel_id"].location == "path"
        assert by_name["channel_id"].required is True
        assert by_name["dry_run"].location == "query"
        assert by_name["dry_run"].required is False

    def test_inlines_request_body_ref(self):
        endpoint = _endpoint(parse_spec(OPENAPI_3), "POST", "/channels/{channel_id}/messages")
        request = next(s for s in endpoint.schemas if s.direction == "request")
        # The $ref must be resolved, not stored as a dangling pointer.
        assert request.json_schema["required"] == ["channel"]
        assert set(request.json_schema["properties"]) == {"channel", "text"}

    def test_captures_response_schema_with_status(self):
        endpoint = _endpoint(parse_spec(OPENAPI_3), "POST", "/channels/{channel_id}/messages")
        response = next(s for s in endpoint.schemas if s.direction == "response")
        assert response.status_code == "201"
        assert "ok" in response.json_schema["properties"]

    def test_flags_delete_as_destructive(self):
        api = parse_spec(OPENAPI_3)
        assert _endpoint(api, "DELETE", "/channels/{channel_id}/messages").is_destructive is True
        assert _endpoint(api, "POST", "/channels/{channel_id}/messages").is_destructive is False

    def test_classifies_auth_schemes_and_their_scopes(self):
        schemes = {s.name: s for s in parse_spec(OPENAPI_3).auth_schemes}
        assert schemes["apiKey"].scheme == "apikey"
        assert schemes["apiKey"].supported is True
        assert schemes["oauth"].scheme == "oauth2"
        assert "chat:write" in schemes["oauth"].scopes
        # oauth2 counts as executable: the browser-based authorization flow is
        # out of scope, but a token the user already holds is sent as a plain
        # bearer header, which is how Slack bot tokens and GitHub PATs work.
        assert schemes["oauth"].supported is True

    def test_unrecognised_scheme_is_unsupported(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Odd", "version": "1"},
            "paths": {"/a": {"get": {"responses": {}}}},
            "components": {"securitySchemes": {"mtls": {"type": "mutualTLS"}}},
        }
        # Request signing and mutual TLS genuinely cannot be performed, so the
        # compiler must still be able to refuse them before execution.
        scheme = parse_spec(spec).auth_schemes[0]
        assert scheme.scheme == "mutualtls"
        assert scheme.supported is False


class TestSwagger2:
    def test_builds_base_url_from_host_and_basepath(self):
        assert parse_spec(SWAGGER_2).base_url == "https://legacy.example.com/api"

    def test_body_parameter_becomes_request_schema(self):
        endpoint = _endpoint(parse_spec(SWAGGER_2), "POST", "/things")
        request = next(s for s in endpoint.schemas if s.direction == "request")
        assert request.json_schema["required"] == ["name"]
        # A body parameter is a schema, not a parameter row.
        assert "body" not in {p.name for p in endpoint.parameters}
        assert "verbose" in {p.name for p in endpoint.parameters}

    def test_reads_response_schema_without_content_block(self):
        endpoint = _endpoint(parse_spec(SWAGGER_2), "POST", "/things")
        response = next(s for s in endpoint.schemas if s.direction == "response")
        assert "id" in response.json_schema["properties"]

    def test_normalizes_basic_auth(self):
        scheme = parse_spec(SWAGGER_2).auth_schemes[0]
        assert scheme.scheme == "basic"
        assert scheme.supported is True


class TestRefResolution:
    def test_breaks_self_referential_cycle(self):
        root = {
            "components": {
                "schemas": {
                    "Node": {
                        "type": "object",
                        "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                    }
                }
            }
        }
        resolved = resolve_refs(root["components"]["schemas"]["Node"], root)
        assert resolved["type"] == "object"  # terminated instead of recursing forever

    def test_deep_plain_nesting_is_not_truncated(self):
        """Ordinary nesting must not consume the cycle budget.

        Counting structural depth toward the reference limit truncated schemas
        mid-object: Google Calendar's event `start` became
        {"properties": {"type": "object"}}, losing date, dateTime and timeZone,
        and the planner then reported those fields did not exist.
        """
        leaf = {"type": "object", "properties": {"dateTime": {"type": "string"}}}
        node = leaf
        for _ in range(12):  # far deeper than MAX_REF_DEPTH, but no references
            node = {"type": "object", "properties": {"child": node}}

        resolved = resolve_refs(node, {})
        cursor = resolved
        for _ in range(12):
            cursor = cursor["properties"]["child"]
        assert cursor["properties"]["dateTime"] == {"type": "string"}

    def test_a_ref_nested_deep_inside_plain_structure_still_resolves(self):
        root = {
            "components": {
                "schemas": {
                    "When": {
                        "type": "object",
                        "properties": {"dateTime": {"type": "string"}},
                    }
                }
            }
        }
        node = {"$ref": "#/components/schemas/When"}
        for _ in range(10):
            node = {"type": "object", "properties": {"child": node}}

        cursor = resolve_refs(node, root)
        for _ in range(10):
            cursor = cursor["properties"]["child"]
        assert "dateTime" in cursor["properties"]

    def test_expansion_is_bounded_in_total_size(self):
        """Cycle detection alone does not bound the output.

        `seen` is per-branch, so sibling properties each re-expand the same
        referenced schema in full. A wide, deep, acyclic reference graph then
        grows combinatorially — unbounded, this corpus reached 5.9 GB.
        """
        import json

        root = {"components": {"schemas": {}}}
        # Each level has 8 properties pointing at the next: 8^n growth.
        for level in range(7):
            root["components"]["schemas"][f"L{level}"] = {
                "type": "object",
                "properties": {
                    f"p{i}": {"$ref": f"#/components/schemas/L{level + 1}"} for i in range(8)
                },
            }
        root["components"]["schemas"]["L7"] = {
            "type": "object",
            "properties": {"leaf": {"type": "string"}},
        }

        resolved = resolve_refs({"$ref": "#/components/schemas/L0"}, root)
        # Bounded, and still a usable object rather than an empty stub.
        assert len(json.dumps(resolved)) < 2_000_000
        assert resolved.get("properties")

    def test_a_long_reference_chain_is_still_cut_off(self):
        # The cycle guard must survive the fix.
        root = {"components": {"schemas": {}}}
        for i in range(20):
            root["components"]["schemas"][f"S{i}"] = {
                "type": "object",
                "properties": {"next": {"$ref": f"#/components/schemas/S{i + 1}"}},
            }
        resolved = resolve_refs({"$ref": "#/components/schemas/S0"}, root)
        assert isinstance(resolved, dict)  # terminated rather than recursing forever

    def test_unresolvable_ref_degrades_to_object(self):
        assert resolve_refs({"$ref": "#/nope/missing"}, {}) == {"type": "object"}

    def test_external_ref_is_not_followed(self):
        assert resolve_refs({"$ref": "https://other.example.com/spec.json"}, {}) == {
            "type": "object"
        }


class TestMalformedSpecs:
    @pytest.mark.parametrize(
        "spec",
        [
            {},
            {"openapi": "3.0.0", "info": {"title": "x"}},
            {"openapi": "3.0.0", "info": {"title": "x"}, "paths": {}},
            {"openapi": "3.0.0", "info": {"title": "x"}, "paths": {"/a": {"get": "not-an-object"}}},
        ],
        ids=["empty", "no-paths", "empty-paths", "no-valid-operations"],
    )
    def test_unusable_specs_raise_spec_error(self, spec):
        # SpecError is caught by the ingestion loop and logged as a skip. Any other
        # exception type would abort the run.
        with pytest.raises(SpecError):
            parse_spec(spec)

    def test_endpoint_cap_is_respected(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Big", "version": "1"},
            "paths": {f"/p{i}": {"get": {"responses": {}}} for i in range(50)},
        }
        assert len(parse_spec(spec, max_endpoints=10).endpoints) == 10
