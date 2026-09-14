"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { call } from "@/lib/api";

type ExchangeResult = {
  status: string;
  provider: string;
  provider_id: string;
  renewable: boolean;
  scopes: string[];
};

function Callback() {
  const params = useSearchParams();
  const [state, setState] = useState<"working" | "done" | "failed">("working");
  const [result, setResult] = useState<ExchangeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = params.get("code");
    const oauthState = params.get("state");
    const denied = params.get("error");

    if (denied) {
      setError(
        denied === "access_denied"
          ? "You declined the permission request, so nothing was connected."
          : denied,
      );
      setState("failed");
      return;
    }
    if (!code || !oauthState) {
      setError("This page expects to be opened by the provider after you approve.");
      setState("failed");
      return;
    }

    // The code is exchanged server-side: the swap needs the client secret,
    // which must never reach the browser.
    call<ExchangeResult>("/v1/oauth/google/exchange", {
      method: "POST",
      body: JSON.stringify({ code, state: oauthState }),
    })
      .then((r) => {
        setResult(r);
        setState("done");
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
        setState("failed");
      });
  }, [params]);

  return (
    <>
      <h1>Connecting your account</h1>

      {state === "working" && (
        <div className="panel">
          <p style={{ marginBottom: 0 }}>Exchanging the sign-in for a token…</p>
        </div>
      )}

      {state === "done" && result && (
        <div className="panel">
          <div className="row">
            <span className="pill ok">connected</span>
            <b>{result.provider_id}</b>
          </div>
          <p className="small" style={{ marginTop: 10 }}>
            The token is encrypted and stored. It will be used only when a workflow
            step calls this provider.
          </p>
          {!result.renewable && (
            <div className="issue" style={{ borderLeftColor: "var(--warn)", background: "var(--warn-bg)" }}>
              <b>No refresh token was issued</b>
              <div className="small">
                This connection will stop working in about an hour. It usually means
                the account had already granted access — revoke it in your provider
                settings and connect again to get a durable token.
              </div>
            </div>
          )}
          {result.scopes.length > 0 && (
            <div className="small muted mono">granted: {result.scopes.join(", ")}</div>
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <Link href="/credentials">
              <button>Back to credentials</button>
            </Link>
            <Link href="/create" className="small">
              build an automation →
            </Link>
          </div>
        </div>
      )}

      {state === "failed" && (
        <div className="panel">
          <div className="issue">
            <b>Could not connect</b>
            <div className="small">{error}</div>
          </div>
          <Link href="/credentials">
            <button className="secondary">Back to credentials</button>
          </Link>
        </div>
      )}
    </>
  );
}

export default function OAuthCallbackPage() {
  return (
    <Suspense fallback={<div className="panel">Loading…</div>}>
      <Callback />
    </Suspense>
  );
}
