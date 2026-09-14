"use client";

import { useEffect, useState } from "react";
import { call, type Health, type SearchResponse } from "@/lib/api";

const MODES = ["hybrid", "bm25", "vector"] as const;

const EXAMPLES = [
  "send a message to a channel",
  "create a new issue in a repository",
  "charge a customer credit card",
  "upload a file to cloud storage",
];

export default function SearchPage() {
  const [query, setQuery] = useState("send a message to a channel");
  const [mode, setMode] = useState<(typeof MODES)[number]>("hybrid");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    call<Health>("/health")
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  async function run(searchQuery = query, searchMode = mode) {
    if (searchQuery.trim().length < 2) return;
    setBusy(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        q: searchQuery,
        mode: searchMode,
        limit: "15",
      });
      setResult(await call<SearchResponse>(`/v1/search?${params}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setResult(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Search the API knowledge base</h1>
      <p className="lede">
        Describe a capability in plain English. Every endpoint here was parsed from a
        real OpenAPI specification — nothing was hand-written.
      </p>

      {health && (
        <div className="panel stat-row">
          <div className="stat">
            <b>{health.endpoints.toLocaleString()}</b>
            <span className="muted small">endpoints</span>
          </div>
          <div className="stat">
            <b>{health.providers}</b>
            <span className="muted small">providers</span>
          </div>
          <div className="stat">
            <b>{health.search_index.toLocaleString()}</b>
            <span className="muted small">indexed</span>
          </div>
          <div className="stat">
            <b>{health.embeddings_available ? "yes" : "no"}</b>
            <span className="muted small">semantic search</span>
          </div>
        </div>
      )}

      <div className="panel">
        <div className="row">
          <input
            type="text"
            value={query}
            placeholder="what should the API do?"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()}
          />
          <select
            value={mode}
            onChange={(e) => {
              const next = e.target.value as (typeof MODES)[number];
              setMode(next);
              if (result) void run(query, next);
            }}
          >
            {MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <button onClick={() => run()} disabled={busy}>
            {busy ? "Searching…" : "Search"}
          </button>
        </div>
        <div className="row small" style={{ marginTop: 10 }}>
          <span className="muted">Try:</span>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              className="secondary small"
              style={{ padding: "3px 9px" }}
              onClick={() => {
                setQuery(example);
                void run(example);
              }}
            >
              {example}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="panel issue">
          <b>Search failed</b>
          <div className="small">{error}</div>
        </div>
      )}

      {result && (
        <div className="panel">
          <p className="muted small" style={{ marginTop: 0 }}>
            {result.count} results for <b>{result.query}</b> using{" "}
            <b>{result.mode}</b> in {result.took_ms} ms
          </p>
          {result.candidates.length === 0 && (
            <p className="muted">Nothing matched. Try different words.</p>
          )}
          {result.candidates.map((candidate) => (
            <div className="result" key={candidate.endpoint_id}>
              <div className="head">
                <span className="muted small">{candidate.rank}.</span>
                <span className="method mono">{candidate.method}</span>
                <span className="mono">{candidate.path}</span>
                <span className="pill">{candidate.provider_id}</span>
                {candidate.is_destructive && (
                  <span className="pill bad">destructive</span>
                )}
                {candidate.is_deprecated && (
                  <span className="pill warn">deprecated</span>
                )}
              </div>
              <div className="small muted">
                {candidate.summary ?? <em>no description published</em>}
              </div>
              <div className="small muted mono">
                {candidate.endpoint_id} · score {candidate.score.toFixed(3)}
                {Object.keys(candidate.component_ranks).length > 0 && (
                  <>
                    {" · "}
                    {Object.entries(candidate.component_ranks)
                      .map(([source, rank]) => `${source} #${rank}`)
                      .join(", ")}
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
