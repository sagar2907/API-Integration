"use client";

import { useState } from "react";
import Link from "next/link";
import {
  call,
  type ExecutionDetail,
  type Issue,
  type PlanResponse,
} from "@/lib/api";
import { ConnectPanel } from "./ConnectPanel";

/** The checks the compiler runs, in the order it runs them. */
const CHECKS: { label: string; codes: string[] }[] = [
  {
    label: "Structure — one trigger, no loops, every step reachable",
    codes: [
      "cycle_detected",
      "no_trigger",
      "multiple_triggers",
      "unreachable_node",
      "duplicate_node_id",
      "unknown_node_reference",
      "malformed_plan",
    ],
  },
  {
    label: "A suitable endpoint exists for what you asked",
    codes: [
      "endpoint_not_found",
      "endpoint_not_in_candidates",
      "missing_endpoint_id",
      "endpoint_deprecated",
      "no_suitable_endpoint",
      "no_candidates",
    ],
  },
  {
    label: "Method and address match our records",
    codes: ["method_mismatch", "path_mismatch", "no_base_url"],
  },
  {
    label: "Every required parameter has a value",
    codes: ["missing_required_parameter", "unknown_parameter", "invalid_target"],
  },
  {
    label: "Every required body field has a value",
    codes: ["missing_required_body_field"],
  },
  {
    label: "Data from one step fits the next",
    codes: ["schema_incompatible", "unresolvable_source_path"],
  },
  {
    label: "Login method supported, dangerous actions gated",
    codes: [
      "unsupported_auth",
      "provider_not_allowlisted",
      "destructive_requires_approval",
    ],
  },
];

function checkStatus(issues: Issue[], codes: string[]) {
  return issues.filter((issue) => codes.includes(issue.code));
}

export default function CreatePage() {
  const [goal, setGoal] = useState(
    "When a new GitHub issue is created, send a Slack message with the issue title",
  );
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [execution, setExecution] = useState<ExecutionDetail | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function makePlan() {
    setBusy("planning");
    setError(null);
    setPlan(null);
    setExecution(null);
    try {
      setPlan(
        await call<PlanResponse>("/v1/workflows/plan", {
          method: "POST",
          body: JSON.stringify({ goal, save: true }),
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function run(dryRun: boolean) {
    if (!plan?.workflow_id) return;
    setBusy(dryRun ? "dry-run" : "run");
    setError(null);
    try {
      setExecution(
        await call<ExecutionDetail>(
          `/v1/workflows/${plan.workflow_id}/executions`,
          { method: "POST", body: JSON.stringify({ dry_run: dryRun }) },
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function refreshConnections() {
    if (!plan?.required_connections?.length) return;
    try {
      const refreshed = await Promise.all(
        plan.required_connections.map((c) =>
          call<(typeof plan.required_connections)[number]>(
            `/v1/providers/${c.provider_id}/connection`,
          ),
        ),
      );
      setPlan({ ...plan, required_connections: refreshed });
    } catch {
      // A failed refresh only means the badges are stale; the plan is intact.
    }
  }

  const issues = plan?.issues ?? [];

  return (
    <>
      <h1>Create an automation</h1>
      <p className="lede">
        Describe what you want. The model proposes a plan using only endpoints the
        search returned, and every claim it makes is checked against the database
        before anything can run.
      </p>

      <div className="panel">
        <textarea value={goal} onChange={(e) => setGoal(e.target.value)} />
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={makePlan} disabled={busy !== null}>
            {busy === "planning" ? "Planning…" : "Plan it"}
          </button>
          <span className="muted small">
            Takes around 20 seconds — it searches, plans, then validates.
          </span>
        </div>
      </div>

      {error && (
        <div className="panel issue">
          <b>Something went wrong</b>
          <div className="small">{error}</div>
        </div>
      )}

      {plan && (
        <>
          <div className="panel">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <b>
                {plan.ok ? (
                  <span className="pill ok">validated</span>
                ) : (
                  <span className="pill bad">rejected</span>
                )}{" "}
                {plan.requirement?.trigger}
              </b>
              <span className="muted small">
                {plan.candidates.length} candidates · {plan.attempts.length} attempt
                {plan.attempts.length === 1 ? "" : "s"} · {plan.tokens} tokens
              </span>
            </div>
            {plan.requirement && (
              <div className="small muted" style={{ marginTop: 6 }}>
                actions: {plan.requirement.actions.join(", ") || "—"}
              </div>
            )}
          </div>

          <ConnectPanel
            connections={plan.required_connections ?? []}
            onChanged={refreshConnections}
          />

          <div className="panel">
            <b>Validation</b>
            <div style={{ marginTop: 8 }}>
              {CHECKS.map((check) => {
                const failures = checkStatus(issues, check.codes);
                const passed = failures.length === 0;
                return (
                  <div className="check" key={check.label}>
                    <span style={{ color: passed ? "var(--ok)" : "var(--bad)" }}>
                      {passed ? "✓" : "✕"}
                    </span>
                    <span className={passed ? "" : "mono"}>{check.label}</span>
                  </div>
                );
              })}
            </div>

            {issues.length > 0 && (
              <div style={{ marginTop: 14 }}>
                {issues.map((issue, index) => (
                  <div className="issue" key={index}>
                    <div className="mono small">
                      {issue.code}
                      {issue.node_id ? ` · step ${issue.node_id}` : ""}
                      {issue.field ? ` · ${issue.field}` : ""}
                    </div>
                    <div>{issue.message}</div>
                    {issue.hint && (
                      <div className="small muted">Hint: {issue.hint}</div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {plan.compiled && (
            <div className="panel">
              <b>The plan</b>
              <p className="small muted" style={{ marginTop: 4 }}>
                The addresses below came from the database, not from the model.
              </p>
              {plan.compiled.execution_order.map((nodeId, index) => {
                const node = plan.compiled!.nodes.find((n) => n.id === nodeId);
                if (!node) return null;
                return (
                  <div key={nodeId}>
                    {index > 0 && <div className="arrow">↓</div>}
                    <div className="node">
                      <div className="head">
                        <span className="pill">{node.type}</span>
                        <span className="method mono">{node.method}</span>
                        <span className="mono small">
                          {node.base_url}
                          {node.path}
                        </span>
                        {node.is_destructive && (
                          <span className="pill bad">destructive</span>
                        )}
                      </div>
                      {node.inputs.map((input, i) => (
                        <div className="small mono muted" key={i}>
                          {input.location}.{input.name} ←{" "}
                          {input.source_node
                            ? `${input.source_node}.${input.source_path}`
                            : JSON.stringify(input.literal)}
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}

              <div className="row" style={{ marginTop: 12 }}>
                <button
                  className="secondary"
                  onClick={() => run(true)}
                  disabled={busy !== null}
                >
                  {busy === "dry-run" ? "Building…" : "Dry run"}
                </button>
                <button onClick={() => run(false)} disabled={busy !== null}>
                  {busy === "run" ? "Running…" : "Run for real"}
                </button>
                <span className="muted small">
                  A dry run builds the real request and sends nothing.
                </span>
              </div>
            </div>
          )}

          {execution && (
            <div className="panel">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <b>
                  <span
                    className={`pill ${
                      execution.status === "succeeded" ? "ok" : "bad"
                    }`}
                  >
                    {execution.status}
                  </span>{" "}
                  {execution.dry_run && <span className="pill">dry run</span>}
                </b>
                <Link className="small" href="/executions">
                  all runs →
                </Link>
              </div>
              {execution.error_message && (
                <div className="issue" style={{ marginTop: 10 }}>
                  <div className="mono small">{execution.error_category}</div>
                  <div>{execution.error_message}</div>
                </div>
              )}
              {execution.nodes.flatMap((node) =>
                (node.warnings ?? []).map((warning, i) => (
                  <div
                    className="issue"
                    key={`${node.node_id}-${i}`}
                    style={{
                      borderLeftColor: "var(--warn)",
                      background: "var(--warn-bg)",
                      marginTop: 10,
                    }}
                  >
                    <div className="mono small">
                      would not run · step {node.node_id}
                    </div>
                    <div>{warning}</div>
                    {warning.includes("credential") && (
                      <div className="small" style={{ marginTop: 4 }}>
                        <Link href="/credentials">Add a token →</Link>
                      </div>
                    )}
                  </div>
                )),
              )}
              <div className="scroll-x">
                <table style={{ marginTop: 10 }}>
                  <thead>
                    <tr>
                      <th>Step</th>
                      <th>Status</th>
                      <th>HTTP</th>
                      <th>Attempts</th>
                      <th>Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {execution.nodes.map((node) => (
                      <tr key={node.node_id}>
                        <td className="mono">{node.node_id}</td>
                        <td>{node.status}</td>
                        <td>{node.status_code ?? "—"}</td>
                        <td>{node.attempts}</td>
                        <td>{node.latency_ms} ms</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </>
  );
}
