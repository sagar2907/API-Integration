/** Client-side access to the engine, always via our own proxy. */

export async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/proxy${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      body?.error?.message ?? body?.detail ?? `request failed (${response.status})`;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return body as T;
}

export type Candidate = {
  endpoint_id: string;
  method: string;
  path: string;
  summary: string | null;
  provider_id: string;
  api_name: string;
  score: number;
  rank: number;
  is_deprecated: boolean;
  is_destructive: boolean;
  matched_terms: string[];
  component_ranks: Record<string, number>;
};

export type SearchResponse = {
  query: string;
  mode: string;
  took_ms: number;
  count: number;
  candidates: Candidate[];
};

export type Issue = {
  code: string;
  message: string;
  node_id?: string | null;
  field?: string | null;
  expected?: string | null;
  actual?: string | null;
  hint?: string | null;
};

export type CompiledNode = {
  id: string;
  type: string;
  method: string | null;
  base_url: string | null;
  path: string | null;
  provider_id: string | null;
  auth_scheme: string | null;
  is_destructive: boolean;
  inputs: {
    location: string;
    name: string;
    source_node: string | null;
    source_path: string | null;
    literal: unknown;
  }[];
};

export type ConnectionStatus = {
  provider_id: string;
  name: string;
  /** connected | paste | oauth | oauth_setup | impossible */
  status: string;
  reason: string;
  scheme: string | null;
  documentation_url: string | null;
  oauth_provider: string | null;
  allowlisted: boolean;
};

export type PlanResponse = {
  ok: boolean;
  goal: string;
  workflow_id: string | null;
  requirement: { trigger: string; actions: string[]; required_fields: string[] } | null;
  compiled: { name: string; nodes: CompiledNode[]; execution_order: string[] } | null;
  candidates: Candidate[];
  attempts: { iteration: number; accepted: boolean; issues: Issue[] }[];
  issues: Issue[];
  tokens: number;
  required_connections: ConnectionStatus[];
};

export type ExecutionSummary = {
  execution_id: string;
  workflow_id: string;
  status: string;
  dry_run: boolean;
  error_category: string | null;
  started_at: string;
};

export type ExecutionDetail = {
  execution_id: string;
  workflow_id: string;
  status: string;
  dry_run: boolean;
  error_category: string | null;
  error_message: string | null;
  nodes: {
    node_id: string;
    status: string;
    status_code: number | null;
    attempts: number;
    latency_ms: number;
    output_fields: string[];
    warnings?: string[];
  }[];
};

export type Health = {
  status: string;
  endpoints: number;
  providers: number;
  search_index: number;
  embeddings_available: boolean;
  llm_configured: boolean;
};
