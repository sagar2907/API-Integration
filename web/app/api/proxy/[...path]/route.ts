/**
 * The backend-for-frontend.
 *
 * Every browser request to the engine goes through here. The purpose is not
 * convenience — it is that the gateway's address and any future session token
 * stay on the server. The browser talks only to this origin, so nothing about
 * how the backend is reached is ever shipped to a client.
 *
 * No business logic belongs in this file. It attaches credentials and forwards.
 */

import { NextRequest, NextResponse } from "next/server";

const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8000";

// Long enough for a planning call, which waits on a language model.
const TIMEOUT_MS = 120_000;

async function forward(request: NextRequest, path: string[]) {
  const target = new URL(`/${path.join("/")}`, GATEWAY_URL);
  request.nextUrl.searchParams.forEach((value, key) => {
    target.searchParams.append(key, value);
  });

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const response = await fetch(target, {
      method: request.method,
      headers: { "Content-Type": "application/json" },
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : await request.text(),
      signal: controller.signal,
      cache: "no-store",
    });

    const body = await response.text();
    return new NextResponse(body, {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : "unknown error";
    // A backend that is simply not running is the most common cause here, and
    // saying so is more useful than a generic 500.
    return NextResponse.json(
      {
        error: {
          code: "gateway_unreachable",
          message: `Could not reach the engine at ${GATEWAY_URL}. Is it running? (${reason})`,
        },
      },
      { status: 502 },
    );
  } finally {
    clearTimeout(timer);
  }
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return forward(request, (await context.params).path);
}

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return forward(request, (await context.params).path);
}
