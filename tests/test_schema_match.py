"""Schema compatibility tests.

The rules encoded here are the ones the compiler enforces, so this file doubles
as the specification for what "compatible" means in this project — including
the cases deliberately left out of scope.
"""

from __future__ import annotations

import pytest

from engine.workflow.schema_match import (
    Compatibility,
    compare,
    required_fields,
    resolve_path,
    schema_type,
)

ISSUE = {
    "type": "object",
    "required": ["title"],
    "properties": {
        "title": {"type": "string"},
        "number": {"type": "integer"},
        "user": {
            "type": "object",
            "required": ["login"],
            "properties": {"login": {"type": "string"}},
        },
        "labels": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
    },
}


class TestSchemaType:
    @pytest.mark.parametrize(
        ("schema", "expected"),
        [
            ({"type": "string"}, "string"),
            ({"properties": {}}, "object"),
            ({"items": {}}, "array"),
            ({"type": ["string", "null"]}, "string"),
            ({}, None),
            ({"type": ["string", "integer"]}, None),
            (None, None),
        ],
    )
    def test_reads_the_declared_type(self, schema, expected):
        assert schema_type(schema) == expected


class TestResolvePath:
    def test_top_level_field(self):
        assert resolve_path(ISSUE, "title") == {"type": "string"}

    def test_nested_field(self):
        # The mistake this project keeps citing: the author is at user.login.
        assert resolve_path(ISSUE, "user.login") == {"type": "string"}

    def test_array_element_field(self):
        assert resolve_path(ISSUE, "labels[].name") == {"type": "string"}

    def test_empty_path_returns_the_whole_schema(self):
        assert resolve_path(ISSUE, "") is ISSUE

    @pytest.mark.parametrize("path", ["author", "user.email", "labels[].colour", "a.b.c"])
    def test_missing_path_returns_none(self, path):
        # Not an error — it means the plan referenced a field that does not exist.
        assert resolve_path(ISSUE, path) is None

    def test_none_schema_is_tolerated(self):
        assert resolve_path(None, "title") is None


class TestCompare:
    def test_identical_primitives_match(self):
        assert compare({"type": "string"}, {"type": "string"}).compatibility is Compatibility.OK

    @pytest.mark.parametrize(
        ("source", "target"),
        [("integer", "number"), ("integer", "string"), ("number", "string"), ("boolean", "string")],
    )
    def test_lossless_conversions_are_allowed(self, source, target):
        result = compare({"type": source}, {"type": target})
        assert result.compatibility is Compatibility.COERCIBLE
        assert result.acceptable

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("string", "number"),
            ("string", "integer"),
            ("object", "string"),
            ("array", "string"),
            ("string", "boolean"),
            ("number", "object"),
        ],
    )
    def test_lossy_or_impossible_conversions_are_refused(self, source, target):
        result = compare({"type": source}, {"type": target})
        assert result.compatibility is Compatibility.INCOMPATIBLE
        assert not result.acceptable

    def test_string_to_number_is_refused_because_text_may_not_be_numeric(self):
        # The asymmetry with integer -> string is deliberate: every number has a
        # string form, but "abc" has no numeric value.
        assert compare({"type": "string"}, {"type": "number"}).compatibility is (
            Compatibility.INCOMPATIBLE
        )

    def test_object_supplying_every_required_field_matches(self):
        source = {"type": "object", "properties": {"channel": {"type": "string"}}}
        target = {"type": "object", "required": ["channel"], "properties": {}}
        assert compare(source, target).compatibility is Compatibility.OK

    def test_object_missing_a_required_field_is_refused(self):
        source = {"type": "object", "properties": {"text": {"type": "string"}}}
        target = {"type": "object", "required": ["channel"], "properties": {}}
        result = compare(source, target)
        assert result.compatibility is Compatibility.INCOMPATIBLE
        assert "channel" in result.detail

    def test_arrays_compare_their_elements(self):
        strings = {"type": "array", "items": {"type": "string"}}
        numbers = {"type": "array", "items": {"type": "number"}}
        assert compare(strings, strings).compatibility is Compatibility.OK
        assert compare(strings, numbers).compatibility is Compatibility.INCOMPATIBLE


class TestUnknownIsPermissive:
    """Where a schema cannot be modelled the checker must not guess.

    This direction is chosen deliberately: the compiler is a gate against
    provably wrong plans, not a proof of correctness. Rejecting everything
    unmodellable would make it useless against real specs.
    """

    def test_missing_type_information_passes(self):
        result = compare({}, {"type": "string"})
        assert result.compatibility is Compatibility.UNKNOWN
        assert result.acceptable

    @pytest.mark.parametrize("keyword", ["oneOf", "anyOf", "allOf"])
    def test_composition_keywords_are_out_of_scope(self, keyword):
        source = {keyword: [{"type": "string"}, {"type": "integer"}]}
        result = compare(source, {"type": "string"})
        assert result.compatibility is Compatibility.UNKNOWN
        assert result.acceptable

    def test_array_with_unknown_elements_passes(self):
        result = compare({"type": "array"}, {"type": "array"})
        assert result.compatibility is Compatibility.UNKNOWN


class TestRequiredFields:
    def test_reads_the_required_list(self):
        assert required_fields(ISSUE) == ["title"]

    @pytest.mark.parametrize("schema", [None, {}, {"required": "title"}, {"required": [1, 2]}])
    def test_malformed_required_lists_yield_nothing(self, schema):
        assert required_fields(schema) == []
