/**
 * Thin server-side client for the Serverless API.
 *
 * All calls run on the server (React Server Components / route handlers) and
 * forward the user's SSO access token as a Bearer, so the API applies the exact
 * same group-based authorization it would for any other OIDC caller. The token
 * never reaches the browser. The `group` query param is the normalized active
 * group - identical to what the API re-normalizes on its side.
 *
 * The shapes below mirror the API's `WorkloadSummary` and `/info` responses
 * (see api/models/common.py, api/models/info.py).
 */

import { getService } from "@/lib/services";

export interface WorkloadSummary {
  name: string;
  group: string;
  type: "function" | "container";
  hostname: string;
  overallStatus: string;
  size: string | null;
  createdAt: string | null;
  sites: string[];
}

export interface PlatformInfo {
  version: string;
  sites: string[];
  runtimes: string[];
  sizes: string[];
  routeDomain: string;
  defaultHostTemplate: string;
}

export class ServerlessApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ServerlessApiError";
  }
}

function baseUrl(): string {
  const svc = getService("serverless");
  if (!svc?.apiBaseUrl) {
    throw new ServerlessApiError(
      "Serverless API address is not configured (PORTAL_SERVERLESS_API_URL).",
    );
  }
  return svc.apiBaseUrl;
}

async function apiGet<T>(path: string, accessToken: string | undefined): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  let resp: Response;
  try {
    resp = await fetch(`${baseUrl()}${path}`, {
      headers,
      // Console data is always live; never serve a cached workload list.
      cache: "no-store",
    });
  } catch (err) {
    throw new ServerlessApiError(`Could not reach the Serverless API: ${(err as Error).message}`);
  }
  if (!resp.ok) {
    throw new ServerlessApiError(`Serverless API returned ${resp.status} for ${path}`, resp.status);
  }
  return (await resp.json()) as T;
}

/** Public platform capabilities (`GET /api/v1/info`); no auth required. */
export function getPlatformInfo(): Promise<PlatformInfo> {
  return apiGet<PlatformInfo>("/api/v1/info", undefined);
}

/** Functions owned by `group` (`GET /api/v1/functions?group=...`). */
export function listFunctions(group: string, accessToken: string | undefined) {
  return apiGet<WorkloadSummary[]>(
    `/api/v1/functions?group=${encodeURIComponent(group)}`,
    accessToken,
  );
}

/** Containers owned by `group` (`GET /api/v1/containers?group=...`). */
export function listContainers(group: string, accessToken: string | undefined) {
  return apiGet<WorkloadSummary[]>(
    `/api/v1/containers?group=${encodeURIComponent(group)}`,
    accessToken,
  );
}

/** All workloads (functions + containers) for a group, merged and sorted. */
export async function listWorkloads(
  group: string,
  accessToken: string | undefined,
): Promise<WorkloadSummary[]> {
  const [functions, containers] = await Promise.all([
    listFunctions(group, accessToken),
    listContainers(group, accessToken),
  ]);
  return [...functions, ...containers].sort((a, b) => a.name.localeCompare(b.name));
}
