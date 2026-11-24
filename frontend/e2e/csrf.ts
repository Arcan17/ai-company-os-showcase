// The app now requires `X-CSRF-Token` on every write that carries a session cookie (the SPA does it in src/api.ts). Specs that write through
// `page.request` / `ctx.request` use this helper, which asks /api/auth/me for the token of the session that context currently holds.
import type { APIRequestContext, APIResponse } from "@playwright/test";

type Ctx = { request: APIRequestContext };

export async function csrfOf(ctx: Ctx, url: string): Promise<string> {
  const base = url.startsWith("http") ? new URL(url).origin : "";
  const r = await ctx.request.get(`${base}/api/auth/me`);
  return r.ok() ? ((await r.json()).csrf_token ?? "") : "";
}

export async function apost(ctx: Ctx, url: string, opts: { data?: unknown; headers?: Record<string, string> } = {}): Promise<APIResponse> {
  const token = await csrfOf(ctx, url);
  return ctx.request.post(url, { ...opts, headers: { ...(opts.headers ?? {}), ...(token ? { "X-CSRF-Token": token } : {}) } });
}
