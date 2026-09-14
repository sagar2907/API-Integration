"use client";

import { useCallback, useEffect, useState } from "react";
import { call, type ExecutionDetail, type ExecutionSummary } from "@/lib/api";

type EventRow = {
  ts: string;
  node_id: string | null;
  event_type: string;
  attempt: number;
  latency_ms: number | null;
  status_code: number | null;
  error_category: string | null;
  payload_metadata: Record<string, unknown>;
};

export default function ExecutionsPage() {
  const [executions, setExecutions] = useState<ExecutionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ExecutionDetail | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const body = await call<{ executions: ExecutionSummary[] }>("/v1/executions");
      setExecutions(body.executions);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function open(executionId: string) {
    if (selected === executionId) {
      setSelected(null);
      return;
    }
    setSelected(executionId);
    try {
      const [d, e] = await Promise.all([
        call<ExecutionDetail>(`/v1/executions/${executionId}`),
        call<{ events: EventRow[] }>(`/v1/executions/${executionId}/events`),
      ]);
      setDetail(d);
      setEvents(e.events);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <h1>Run history</h1>
      <p className="lede">
        Every run of every automation, with a per-step timeline. Request and response
        bodies are never stored — only shapes, sizes and timings.
      </p>

      {error && (
        <div className="panel issue">
          <b>Could not load runs</b>
          <div className="small">{error}</div>
        </div>
      )}

      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <b>{executions.length} runs</b>
          <button className="secondary" onClick={() => void load()}>
            Refresh
          </button>
        </div>

        {executions.length === 0 && (
          <p className="muted" style={{ marginBottom: 0 }}>
            Nothing has run yet. Create an automation and run it.
          </p>
        )}

        {executions.map((execution) => (
          <div className="result" key={execution.execution_id}>
            <div
              className="head"
              style={{ cursor: "pointer" }}
              onClick={() => void open(execution.execution_id)}
            >
              <span
                className={`pill ${
                  execution.status === "succeeded"
                    ? "ok"
                    : execution.status === "failed"
                      ? "bad"
                      : ""
                }`}
              >
                {execution.status}
              </span>
              <span className="mono small">{execution.execution_id}</span>
              {execution.dry_run && <span className="pill">dry run</span>}
              {execution.error_category && (
                <span className="pill bad">{execution.error_category}</span>
              )}
              <span className="muted small">
                {new Date(execution.started_at).toLocaleString()}
              </span>
            </div>

            {selected === execution.execution_id && detail && (
              <div style={{ marginTop: 10 }}>
                {detail.error_message && (
                  <div className="issue">
                    <div className="mono small">{detail.error_category}</div>
                    <div className="small">{detail.error_message}</div>
                  </div>
                )}
                <div className="scroll-x">
                  <table>
                    <thead>
                      <tr>
                        <th>Time</th>
                        <th>Step</th>
                        <th>Event</th>
                        <th>Attempt</th>
                        <th>HTTP</th>
                        <th>Latency</th>
                      </tr>
                    </thead>
                    <tbody>
                      {events.map((event, index) => (
                        <tr key={index}>
                          <td className="mono small">
                            {new Date(event.ts).toLocaleTimeString()}
                          </td>
                          <td className="mono">{event.node_id ?? "—"}</td>
                          <td>
                            {event.event_type}
                            {event.error_category && (
                              <span className="pill bad" style={{ marginLeft: 6 }}>
                                {event.error_category}
                              </span>
                            )}
                          </td>
                          <td>{event.attempt}</td>
                          <td>{event.status_code ?? "—"}</td>
                          <td>
                            {event.latency_ms != null ? `${event.latency_ms} ms` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </>
  );
}
