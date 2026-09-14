"use client";

import { useState } from "react";
import { call, type ConnectionStatus } from "@/lib/api";
import { guideFor } from "@/lib/providers";

type OAuthStart = {
  configured: boolean;
  authorize_url: string | null;
  redirect_uri: string;
  scopes: string[];
};

/**
 * Asks for exactly the services this plan needs.
 *
 * The alternative — a catalogue of every indexed provider — puts the work of
 * figuring out which ones matter onto the person who just described what they
 * wanted in a sentence. The plan already knows, so it asks.
 *
 * Anything that cannot be connected says so plainly here, rather than looking
 * connectable and failing later.
 */
export function ConnectPanel({
  connections,
  onChanged,
}: {
  connections: ConnectionStatus[];
  onChanged: () => void;
}) {
  if (connections.length === 0) return null;

  const outstanding = connections.filter((c) => c.status !== "connected");
  const blocked = connections.filter((c) => c.status === "impossible");

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <b>Services this needs</b>
        <span className="muted small">
          {connections.length - outstanding.length} of {connections.length} connected
        </span>
      </div>

      {outstanding.length === 0 && (
        <p className="small" style={{ color: "var(--ok)", marginBottom: 0 }}>
          Everything this workflow calls is connected. It can be run for real.
        </p>
      )}

      {blocked.length > 0 && (
        <p className="small" style={{ marginTop: 6 }}>
          {blocked.length === connections.length
            ? "This automation cannot be connected — see below."
            : "Some steps can be connected; others cannot."}
        </p>
      )}

      {connections.map((connection) => (
        <ConnectRow
          key={connection.provider_id}
          connection={connection}
          onChanged={onChanged}
        />
      ))}
    </div>
  );
}

function ConnectRow({
  connection,
  onChanged,
}: {
  connection: ConnectionStatus;
  onChanged: () => void;
}) {
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [oauth, setOauth] = useState<OAuthStart | null>(null);
  const [loadingOauth, setLoadingOauth] = useState(false);
  const [allowing, setAllowing] = useState(false);
  const guide = guideFor(connection.provider_id);
  const label = guide?.name ?? connection.name ?? connection.provider_id;

  async function save() {
    if (!secret.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await call("/v1/credentials", {
        method: "POST",
        body: JSON.stringify({
          provider_id: connection.provider_id,
          secret,
          scheme: connection.scheme ?? "bearer",
        }),
      });
      setSecret("");
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function allow() {
    setAllowing(true);
    setError(null);
    try {
      await call(`/v1/providers/${connection.provider_id}/allowlist`, {
        method: "POST",
        body: JSON.stringify({ allowed: true }),
      });
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAllowing(false);
    }
  }

  async function startOauth() {
    if (!connection.oauth_provider) return;
    setLoadingOauth(true);
    try {
      const start = await call<OAuthStart>(
        `/v1/oauth/${connection.oauth_provider}/start`,
      );
      if (start.configured && start.authorize_url) {
        window.location.href = start.authorize_url;
        return;
      }
      setOauth(start);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingOauth(false);
    }
  }

  const pill =
    connection.status === "connected"
      ? "ok"
      : connection.status === "impossible"
        ? "bad"
        : "warn";

  return (
    <div className="result">
      <div className="head">
        <span className={`pill ${pill}`}>
          {connection.status === "connected"
            ? "connected"
            : connection.status === "impossible"
              ? "cannot connect"
              : "needs access"}
        </span>
        <b>{label}</b>
        <span className="mono small muted">{connection.provider_id}</span>
      </div>

      <div className="small muted" style={{ marginTop: 4 }}>
        {connection.reason}
      </div>

      {/* Written steps, when we have them. */}
      {connection.status !== "connected" &&
        connection.status !== "impossible" &&
        guide?.steps && (
          <details style={{ marginTop: 6 }}>
            <summary className="small" style={{ cursor: "pointer" }}>
              How to get a token for {label}
            </summary>
            <ol className="small" style={{ paddingLeft: 20, marginTop: 6 }}>
              {guide.steps.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
            {guide.tokenUrl && (
              <a href={guide.tokenUrl} target="_blank" rel="noreferrer" className="small">
                open {new URL(guide.tokenUrl).hostname} →
              </a>
            )}
          </details>
        )}

      {/* No steps written: point at whatever documentation the spec gave us. */}
      {connection.status === "paste" && !guide?.steps && connection.documentation_url && (
        <div className="small" style={{ marginTop: 4 }}>
          <a href={connection.documentation_url} target="_blank" rel="noreferrer">
            {label} documentation →
          </a>{" "}
          <span className="muted">
            look for &quot;API key&quot; or &quot;personal access token&quot;
          </span>
        </div>
      )}

      {connection.status === "paste" && (
        <div className="row" style={{ marginTop: 8 }}>
          <input
            type="password"
            value={secret}
            placeholder={`${connection.scheme ?? "bearer"} token for ${label}`}
            onChange={(e) => setSecret(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            style={{
              flex: 1,
              minWidth: 220,
              font: "inherit",
              color: "inherit",
              background: "var(--bg)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: "7px 11px",
            }}
          />
          <button onClick={save} disabled={busy || !secret.trim()}>
            {busy ? "Saving…" : "Connect"}
          </button>
        </div>
      )}

      {(connection.status === "oauth" || connection.status === "oauth_setup") && (
        <div style={{ marginTop: 8 }}>
          <button onClick={startOauth} disabled={loadingOauth}>
            {loadingOauth ? "Checking…" : `Sign in with ${label}`}
          </button>
          {oauth && !oauth.configured && (
            <div
              className="issue"
              style={{
                borderLeftColor: "var(--warn)",
                background: "var(--warn-bg)",
                marginTop: 8,
              }}
            >
              <b>One-time registration needed first</b>
              <ol className="small" style={{ paddingLeft: 18, marginBottom: 6 }}>
                {guide?.oauthSetup?.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              <div className="small">Redirect URI must match exactly:</div>
              <div className="mono small">{oauth.redirect_uri}</div>
            </div>
          )}
        </div>
      )}

      {connection.status !== "impossible" && !connection.allowlisted && (
        <div className="row small" style={{ marginTop: 8, alignItems: "flex-start" }}>
          <button
            className="secondary"
            style={{ padding: "3px 10px" }}
            disabled={allowing}
            onClick={allow}
          >
            {allowing ? "Enabling…" : "Allow real calls"}
          </button>
          <span className="muted" style={{ flex: 1, minWidth: 240 }}>
            Not yet permitted to be called for real, so this step is dry-run only.
            This is a separate safety catch from the token: it stops a mis-planned
            workflow reaching a service you never intended.
          </span>
        </div>
      )}

      {error && (
        <div className="small" style={{ color: "var(--bad)", marginTop: 6 }}>
          {error}
        </div>
      )}
    </div>
  );
}
