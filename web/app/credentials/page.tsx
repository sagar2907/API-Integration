"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { call } from "@/lib/api";
import { guideFor, type ProviderGuide } from "@/lib/providers";

type Credential = {
  credential_id: string;
  provider_id: string;
  scheme: string;
  label: string | null;
  created_at: string;
};

type Scheme = {
  scheme: string;
  location: string | null;
  parameter_name: string | null;
  supported: boolean;
};

type Connectable = {
  provider_id: string;
  name: string;
  endpoints: number;
  documentation_url: string | null;
  base_url: string | null;
  has_credential: boolean;
  allowlisted: boolean;
  schemes: Scheme[];
  token_in_url: boolean;
};

type OAuthStart = {
  configured: boolean;
  authorize_url: string | null;
  redirect_uri: string;
  scopes: string[];
};

/** What the specification says the provider expects, in plain words. */
function describeAuth(provider: Connectable): string {
  if (provider.schemes.length === 0) {
    return "This specification does not say how to authenticate. That does not mean none is needed — GitHub's own document omits it entirely while plainly requiring a token. Check the documentation.";
  }
  const parts = provider.schemes.map((s) => {
    if (s.scheme === "oauth2")
      return "OAuth 2.0 (a sign-in flow, not a key you can paste)";
    if (s.scheme === "apikey")
      return `an API key sent as ${s.location ?? "header"} ${s.parameter_name ?? ""}`.trim();
    if (s.scheme === "basic") return "a username and password (basic auth)";
    return `a ${s.scheme} token`;
  });
  return `Expects ${parts.join(", or ")}.`;
}

function defaultScheme(provider: Connectable, guide?: ProviderGuide): string {
  if (guide) return guide.scheme;
  const supported = provider.schemes.find((s) => s.supported && s.scheme !== "oauth2");
  return supported?.scheme === "apikey"
    ? "apikey"
    : supported?.scheme === "basic"
      ? "basic"
      : "bearer";
}

export default function CredentialsPage() {
  const [providers, setProviders] = useState<Connectable[]>([]);
  const [items, setItems] = useState<Credential[]>([]);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [secret, setSecret] = useState("");
  const [scheme, setScheme] = useState("bearer");
  const [oauth, setOauth] = useState<OAuthStart | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, c] = await Promise.all([
        call<Connectable[]>("/v1/providers/connectable"),
        call<Credential[]>("/v1/credentials"),
      ]);
      setProviders(p);
      setItems(c);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const selected = useMemo(
    () => providers.find((p) => p.provider_id === selectedId) ?? null,
    [providers, selectedId],
  );
  const guide = selected ? guideFor(selected.provider_id) : undefined;

  useEffect(() => {
    if (!selected) return;
    setScheme(defaultScheme(selected, guide));
    setSecret("");
    setSaved(null);
    if (guide?.oauth) {
      call<OAuthStart>(`/v1/oauth/${guide.oauth}/start`)
        .then(setOauth)
        .catch(() => setOauth(null));
    } else {
      setOauth(null);
    }
  }, [selected, guide]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return providers.slice(0, 40);
    return providers
      .filter(
        (p) =>
          p.provider_id.toLowerCase().includes(q) || p.name.toLowerCase().includes(q),
      )
      .slice(0, 40);
  }, [providers, query]);

  async function save() {
    if (!selected || !secret.trim()) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      await call<Credential>("/v1/credentials", {
        method: "POST",
        body: JSON.stringify({
          provider_id: selected.provider_id,
          secret,
          scheme,
        }),
      });
      setSecret(""); // no reason for it to linger in the page
      setSaved(selected.name);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await call(`/v1/credentials/${id}`, { method: "DELETE" });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const oauthOnly =
    selected != null &&
    selected.schemes.length > 0 &&
    selected.schemes.every((s) => s.scheme === "oauth2") &&
    !guide?.oauth;

  return (
    <>
      <h1>Connect a service</h1>
      <p className="lede">
        Any of the {providers.length} indexed providers can be connected. Tokens are
        encrypted before storage, decrypted only at the moment of an outbound call,
        and never shown to the language model or written to a log.
      </p>

      <div className="panel">
        <div className="row">
          <input
            type="text"
            value={query}
            placeholder={`search ${providers.length} providers — telegram, notion, zoom…`}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="secondary" onClick={() => void load()}>
            Refresh
          </button>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          {filtered.map((p) => (
            <button
              key={p.provider_id}
              className={p.provider_id === selectedId ? "" : "secondary"}
              style={{ padding: "5px 11px" }}
              onClick={() => setSelectedId(p.provider_id)}
              title={`${p.endpoints} endpoints`}
            >
              {guideFor(p.provider_id)?.name ?? p.provider_id}
              {p.has_credential && " ✓"}
            </button>
          ))}
        </div>
        {filtered.length === 0 && (
          <p className="muted small" style={{ marginBottom: 0 }}>
            Nothing matches. Only providers already ingested appear here — run{" "}
            <span className="mono">engine fetch</span> to add more.
          </p>
        )}
      </div>

      {selected && (
        <div className="panel">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>{guide?.name ?? selected.name}</b>
            <span className="muted small mono">
              {selected.provider_id} · {selected.endpoints} endpoints
            </span>
          </div>

          <p className="small" style={{ marginTop: 8 }}>
            {describeAuth(selected)}
          </p>

          {selected.token_in_url && (
            <div
              className="issue"
              style={{ borderLeftColor: "var(--warn)", background: "var(--warn-bg)" }}
            >
              <b>This provider puts the token in the URL</b>
              <div className="small">
                Its address is <span className="mono">{selected.base_url}</span>. Requests
                are built from indexed metadata and the engine does not substitute into
                the host, so this provider cannot be executed yet even with a token
                stored.
              </div>
            </div>
          )}

          {/* Steps we have written out, when we have them. */}
          {guide?.steps && (
            <>
              <div className="row" style={{ justifyContent: "space-between", marginTop: 6 }}>
                <b className="small">How to get a token</b>
                {guide.tokenUrl && (
                  <a href={guide.tokenUrl} target="_blank" rel="noreferrer" className="small">
                    open {new URL(guide.tokenUrl).hostname} →
                  </a>
                )}
              </div>
              <ol className="small" style={{ paddingLeft: 20 }}>
                {guide.steps.map((step, i) => (
                  <li key={i} style={{ marginBottom: 3 }}>
                    {step}
                  </li>
                ))}
              </ol>
              {guide.note && (
                <p className="small muted">{guide.note}</p>
              )}
            </>
          )}

          {/* No written steps: point at the provider's own documentation. */}
          {!guide?.steps && (
            <p className="small muted">
              {selected.documentation_url ? (
                <>
                  We have no written walkthrough for this one. Its own documentation is
                  at{" "}
                  <a href={selected.documentation_url} target="_blank" rel="noreferrer">
                    {new URL(selected.documentation_url).hostname}
                  </a>
                  ; look for &quot;API key&quot;, &quot;personal access token&quot; or
                  &quot;developer settings&quot;, then paste it below.
                </>
              ) : (
                <>
                  We have no written walkthrough and the specification lists no
                  documentation link. Search for &quot;{selected.name} API token&quot;,
                  then paste it below.
                </>
              )}
            </p>
          )}

          {/* OAuth providers we support the flow for. */}
          {guide?.oauth && oauth?.configured && (
            <div className="row" style={{ marginTop: 10 }}>
              <a href={oauth.authorize_url ?? "#"}>
                <button>Sign in with {guide.name}</button>
              </a>
              <span className="muted small">
                requesting {oauth.scopes.map((s) => s.split("/").pop()).join(", ")}
              </span>
            </div>
          )}
          {guide?.oauth && oauth && !oauth.configured && (
            <div className="issue" style={{ borderLeftColor: "var(--warn)", background: "var(--warn-bg)" }}>
              <b>One-time setup needed</b>
              <ol className="small" style={{ paddingLeft: 18, marginBottom: 6 }}>
                {guide.oauthSetup?.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              <div className="small">Redirect URI must match exactly:</div>
              <div className="mono small">{oauth.redirect_uri}</div>
            </div>
          )}

          {/* OAuth-only, and we have no flow for it. */}
          {oauthOnly && (
            <div className="issue" style={{ borderLeftColor: "var(--warn)", background: "var(--warn-bg)" }}>
              <b>This provider only issues tokens through OAuth</b>
              <div className="small">
                There is no key to paste, and no sign-in flow is configured for it here.
                If you already hold a token for it from elsewhere, pasting it below will
                work.
              </div>
            </div>
          )}

          {/* Everyone gets the paste box: a token obtained any way still works. */}
          <div className="row" style={{ marginTop: 12 }}>
            <input
              type="password"
              value={secret}
              placeholder={`token for ${guide?.name ?? selected.name}`}
              onChange={(e) => setSecret(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && save()}
              style={{
                flex: 1,
                minWidth: 240,
                font: "inherit",
                color: "inherit",
                background: "var(--bg)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: "9px 12px",
              }}
            />
            <select value={scheme} onChange={(e) => setScheme(e.target.value)}>
              <option value="bearer">bearer</option>
              <option value="apikey">apikey</option>
              <option value="basic">basic</option>
            </select>
            <button onClick={save} disabled={busy || !secret.trim()}>
              {busy ? "Saving…" : selected.has_credential ? "Replace" : "Save"}
            </button>
          </div>

          {saved && (
            <div className="small" style={{ color: "var(--ok)", marginTop: 8 }}>
              Stored for {saved}, encrypted. It cannot be read back from this page.
            </div>
          )}

          <div className="row small" style={{ marginTop: 10 }}>
            <span className={`pill ${selected.has_credential ? "ok" : "bad"}`}>
              {selected.has_credential ? "token stored" : "no token yet"}
            </span>
            {selected.allowlisted ? (
              <span className="pill ok">allowed to run</span>
            ) : (
              <button
                className="secondary"
                style={{ padding: "2px 10px" }}
                onClick={async () => {
                  await call(`/v1/providers/${selected.provider_id}/allowlist`, {
                    method: "POST",
                    body: JSON.stringify({ allowed: true }),
                  });
                  await load();
                }}
              >
                Allow real calls
              </button>
            )}
          </div>
        </div>
      )}

      {error && (
        <div className="panel issue">
          <b>Something went wrong</b>
          <div className="small">{error}</div>
        </div>
      )}

      <div className="panel">
        <b>{items.length} connected</b>
        {items.length === 0 && (
          <p className="muted" style={{ marginBottom: 0 }}>
            Nothing connected yet. Without a token a workflow can still be planned,
            validated and dry-run — it just cannot actually call anything.
          </p>
        )}
        {items.map((item) => (
          <div className="result" key={item.credential_id}>
            <div className="head">
              <span>{guideFor(item.provider_id)?.name ?? item.provider_id}</span>
              <span className="mono small muted">{item.provider_id}</span>
              <span className="pill">{item.scheme}</span>
              {item.label && <span className="pill">{item.label}</span>}
              <button
                className="secondary small"
                style={{ padding: "2px 8px", marginLeft: "auto" }}
                onClick={() => void remove(item.credential_id)}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
