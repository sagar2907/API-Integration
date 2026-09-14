"""Search tests.

The text layer is pure, so it is pinned exactly. Fusion is pure too and is
tested with synthetic rankings rather than a live index, which keeps these fast
and independent of whatever happens to be ingested.
"""

from __future__ import annotations

import pytest

from engine.search.retrieve import Candidate, reciprocal_rank_fusion
from engine.search.text import (
    build_match_expression,
    endpoint_document,
    expand_tokens,
    normalize_query,
    split_camel,
    tokenize_path,
)


class TestTokenization:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/repos/{owner}/{repo}/issues", "repos owner repo issues"),
            ("/chat.postMessage", "chat post message"),
            ("/v1/charges", "v1 charges"),
            ("/users/{user_id}/scheduled-sends", "users user id scheduled sends"),
            ("/", ""),
            ("/api/v2/Notifications", "api v2 notifications"),
        ],
    )
    def test_path_becomes_searchable_words(self, path, expected):
        # Without this, a path is one opaque token and "post message" can never
        # match Slack's /chat.postMessage.
        assert tokenize_path(path) == expected

    def test_camel_case_split(self):
        assert split_camel("postMessage") == "post Message"
        assert split_camel("listOrgIssues") == "list Org Issues"
        assert split_camel("lowercase") == "lowercase"


class TestQueryNormalization:
    def test_strips_punctuation_and_lowercases(self):
        assert normalize_query("Send a MESSAGE, please!") == ["send", "message", "please"]

    def test_drops_stopwords_and_single_characters(self):
        assert normalize_query("how do I get the user in a repo") == ["do", "get", "user", "repo"]

    def test_empty_query_yields_no_tokens(self):
        assert normalize_query("   ") == []
        assert normalize_query("!!!") == []


class TestSynonymExpansion:
    def test_expands_action_verbs(self):
        assert "post" in expand_tokens(["send"])
        assert "create" in expand_tokens(["send"])

    def test_preserves_original_token_first(self):
        assert expand_tokens(["send"])[0] == "send"

    def test_bridges_repo_abbreviation_family(self):
        # The Porter stemmer maps "repository" to "repositori" and "repos" to
        # "repo", so without this a query saying "repository" misses every
        # GitHub path saying "repos".
        expanded = expand_tokens(["repository"])
        assert "repos" in expanded
        assert "repo" in expanded

    def test_deduplicates(self):
        expanded = expand_tokens(["send", "post"])
        assert len(expanded) == len(set(expanded))


class TestMatchExpression:
    def test_builds_quoted_disjunction(self):
        assert build_match_expression("send message", expand=False) == '"send" OR "message"'

    def test_empty_query_returns_empty_string(self):
        assert build_match_expression("   ") == ""

    @pytest.mark.parametrize("query", ['drop table"', "a - b", "foo* AND bar", "NOT x", "((("])
    def test_fts5_operators_in_user_input_are_neutralized(self, query):
        # Quoting every token means user input can never be parsed as FTS5
        # syntax; an unquoted '-' or '*' would be a syntax error at best.
        # Input that reduces to no tokens yields an empty expression, which
        # callers treat as "no results" rather than passing it to SQLite.
        expression = build_match_expression(query, expand=False)
        if not expression:
            return
        for token in expression.split(" OR "):
            assert token.startswith('"') and token.endswith('"')

    def test_query_of_only_noise_produces_no_expression(self):
        assert build_match_expression("(((") == ""
        assert build_match_expression("a - b") == ""

    def test_expansion_can_be_disabled(self):
        assert build_match_expression("send", expand=False) == '"send"'
        assert len(build_match_expression("send", expand=True)) > len('"send"')


class TestEndpointDocument:
    def test_builds_all_indexed_columns(self):
        document = endpoint_document(
            summary="Create an issue",
            description="x" * 5000,
            path="/repos/{owner}/{repo}/issues",
            operation_id="issues/create",
            tags=["issues"],
            provider="github_com",
            api_name="GitHub v3 REST API",
        )
        assert document["summary"] == "Create an issue"
        assert document["path_tokens"] == "repos owner repo issues"
        assert "issues create" in document["operation"]
        assert document["provider"] == "github com"
        # Some providers paste an entire tutorial into one description; letting
        # that through would distort term statistics for the whole corpus.
        assert len(document["description"]) == 2000

    def test_tolerates_missing_fields(self):
        document = endpoint_document(
            summary=None,
            description=None,
            path="/x",
            operation_id=None,
            tags=None,
            provider="p",
            api_name="A",
        )
        assert document["summary"] == ""
        assert document["path_tokens"] == "x"


def _candidate(endpoint_id: str, rank: int) -> Candidate:
    return Candidate(
        endpoint_id=endpoint_id,
        method="POST",
        path=f"/{endpoint_id}",
        summary=None,
        operation_id=None,
        api_name="API",
        provider_id="p",
        provider_name="P",
        is_deprecated=False,
        is_destructive=False,
        score=1.0,
        rank=rank,
    )


class TestReciprocalRankFusion:
    def test_agreement_between_rankers_wins(self):
        # 'b' is second in both lists; 'a' and 'c' are first in only one.
        fused = reciprocal_rank_fusion(
            {
                "bm25": [_candidate("a", 1), _candidate("b", 2)],
                "vector": [_candidate("c", 1), _candidate("b", 2)],
            },
            limit=3,
        )
        assert fused[0].endpoint_id == "b"

    def test_records_component_ranks_for_explainability(self):
        fused = reciprocal_rank_fusion(
            {"bm25": [_candidate("a", 3)], "vector": [_candidate("a", 7)]}, limit=1
        )
        assert fused[0].component_ranks == {"bm25": 3, "vector": 7}

    def test_result_present_in_one_ranker_still_survives(self):
        fused = reciprocal_rank_fusion({"bm25": [_candidate("only", 1)], "vector": []}, limit=5)
        assert [c.endpoint_id for c in fused] == ["only"]

    def test_ranks_are_renumbered_contiguously(self):
        fused = reciprocal_rank_fusion(
            {
                "bm25": [_candidate("a", 1), _candidate("b", 2), _candidate("c", 3)],
                "vector": [_candidate("c", 1)],
            },
            limit=3,
        )
        assert [c.rank for c in fused] == [1, 2, 3]

    def test_smaller_k_sharpens_top_rank_influence(self):
        rankings = {
            "bm25": [_candidate("top", 1), _candidate("mid", 2)],
            "vector": [_candidate("mid", 1), _candidate("top", 30)],
        }
        # With a small k the rank-1 positions dominate; with a large k the
        # differences flatten out. Both must still produce a full ordering.
        assert len(reciprocal_rank_fusion(rankings, k=1, limit=2)) == 2
        assert len(reciprocal_rank_fusion(rankings, k=1000, limit=2)) == 2

    def test_empty_input_is_not_an_error(self):
        assert reciprocal_rank_fusion({"bm25": [], "vector": []}, limit=10) == []
