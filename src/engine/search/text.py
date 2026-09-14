"""Query and document text preparation.

Pure functions, no I/O — this is the part of retrieval that unit tests can pin
down exactly, and getting it wrong silently destroys recall.
"""

from __future__ import annotations

import re

# postMessage -> post Message, listOrgIssues -> list Org Issues
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PATH_SEPARATORS = re.compile(r"[/._\-:]+")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")

# Function words carry no retrieval signal but still widen the OR expression.
# BM25's IDF makes them nearly weightless rather than harmful, so this is a
# tidiness and latency measure more than a relevance one.
STOPWORDS = frozenset(
    """a an and are as at be by for from has have how i in into is it its of on
    or that the this to want was what when where which who will with would my me
    our your their""".split()
)

# Users describe intent with verbs that rarely appear in API documentation.
# Nobody writes "notify" in an OpenAPI summary; they write "post a message".
# Expansion is additive — BM25's IDF weighting keeps the rarer original terms
# dominant, so a synonym helps recall without swamping precision.
#
# repo/repos/repository is an abbreviation family the Porter stemmer cannot
# bridge: "repository" stems to "repositori" while "repos" stems to "repo", so
# a query saying "repository" silently misses every GitHub path saying "repos".
ACTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "send": ("post", "create", "deliver"),
    "notify": ("message", "post", "alert", "notification"),
    "alert": ("notify", "notification"),
    "message": ("post", "chat", "notification"),
    "add": ("create", "post", "insert"),
    "make": ("create", "post"),
    "new": ("create",),
    "remove": ("delete", "destroy"),
    "cancel": ("delete", "void"),
    "fetch": ("get", "list", "retrieve"),
    "read": ("get", "retrieve"),
    "find": ("search", "list", "query"),
    "show": ("get", "list"),
    "change": ("update", "patch", "modify"),
    "edit": ("update", "patch"),
    "upload": ("create", "post", "file"),
    "issue": ("issues", "ticket", "bug"),
    "ticket": ("issue", "issues"),
    "channel": ("conversation", "room"),
    "repo": ("repos", "repository", "repositories"),
    "repos": ("repo", "repository"),
    "repository": ("repo", "repos"),
    "repositories": ("repo", "repos"),
    "user": ("member", "account"),
    "email": ("mail", "message"),
}


def split_camel(value: str) -> str:
    return _CAMEL_BOUNDARY.sub(" ", value)


def tokenize_path(path: str) -> str:
    """Turn an API path into searchable words.

    `/repos/{owner}/{repo}/issues` -> `repos owner repo issues`
    `/chat.postMessage`            -> `chat post message`

    Without this the path is one opaque token and a search for "post message"
    can never match Slack's endpoint.
    """
    cleaned = path.replace("{", " ").replace("}", " ")
    words: list[str] = []
    for part in _PATH_SEPARATORS.split(cleaned):
        if not part:
            continue
        words.extend(split_camel(part).split())
    return " ".join(word.lower() for word in words if word)


def normalize_query(query: str) -> list[str]:
    """Lowercase, strip punctuation, and split into tokens."""
    lowered = _NON_WORD.sub(" ", query.lower())
    return [
        token
        for token in _WHITESPACE.split(lowered)
        if token and token not in STOPWORDS and len(token) > 1
    ]


# Measured experiment, 2026-08-22: making these relations symmetric (deriving
# bug -> issue from issue -> bug, and so on) is theoretically correct — synonymy
# *is* symmetric — and measurably worse. It fixed one query and broke five,
# taking BM25 NDCG@10 from 0.301 to 0.241 and misses from 8 to 11. Expansion is
# not free: every extra term dilutes the signal, and symmetrising produced long
# cascades like user -> member -> account. Kept deliberately asymmetric. See
# bench/RESULTS.md.


def expand_tokens(tokens: list[str]) -> list[str]:
    """Add action-verb synonyms, preserving order and removing duplicates."""
    expanded: list[str] = []
    for token in tokens:
        if token not in expanded:
            expanded.append(token)
        for synonym in ACTION_SYNONYMS.get(token, ()):
            if synonym not in expanded:
                expanded.append(synonym)
    return expanded


def build_match_expression(query: str, *, expand: bool = True) -> str:
    """Build an FTS5 MATCH expression from free text.

    Every token is quoted, which both escapes FTS5 operators (a stray `-` or
    `*` in user input would otherwise be a syntax error) and keeps the
    expression a plain disjunction. OR rather than AND because recall matters
    more than precision at this stage — BM25 ranking sorts out relevance, and
    the top-K set is handed to a reranking step afterwards.
    """
    tokens = normalize_query(query)
    if not tokens:
        return ""
    if expand:
        tokens = expand_tokens(tokens)
    quoted = [f'"{token}"' for token in tokens]
    return " OR ".join(quoted)


def endpoint_document(
    *,
    summary: str | None,
    description: str | None,
    path: str,
    operation_id: str | None,
    tags: list[str] | None,
    provider: str,
    api_name: str,
) -> dict[str, str]:
    """Assemble the per-column text indexed for one endpoint.

    Descriptions are truncated: some providers paste an entire tutorial into
    one operation's description, which would dominate the term statistics for
    the whole corpus.
    """
    return {
        "summary": (summary or "").strip(),
        "operation": " ".join(
            filter(None, [split_camel(operation_id or "").replace("/", " "), api_name])
        ).strip(),
        "path_tokens": tokenize_path(path),
        "description": (description or "").strip()[:2000],
        "tags": " ".join(tags or []),
        "provider": provider.replace("_", " "),
    }
