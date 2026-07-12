import { auth } from "@/auth";
import { getService } from "@/lib/services";
import { resolveActiveGroup } from "@/lib/session-group";
import {
  getPlatformInfo,
  listWorkloads,
  ServerlessApiError,
  type PlatformInfo,
  type WorkloadSummary,
} from "@/lib/serverless";

export const metadata = { title: "Serverless" };

// Always render live against the API for the active group.
export const dynamic = "force-dynamic";

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "Ready"
      ? "ok"
      : status === "Failed" || status === "Degraded"
        ? "error"
        : status === "Deploying" || status === "Pending"
          ? "warn"
          : "muted";
  return <span className={`pill pill--${tone}`}>{status}</span>;
}

/**
 * The Serverless offering page. Reads the active group from the cookie and
 * lists that group's functions and containers from the Serverless API, calling
 * it server-side with the user's SSO access token (same group-based authz the
 * API applies to any OIDC caller). Also surfaces the platform capabilities from
 * the public `/info` endpoint as context chips.
 */
export default async function ServerlessPage() {
  const session = await auth();
  const svc = getService("serverless");
  const groups = session?.user.groups ?? [];
  const activeGroup = await resolveActiveGroup(groups);

  if (!svc?.enabled) {
    return (
      <div className="page">
        <div className="page__header">
          <h1 className="page__title">⚡ Serverless</h1>
        </div>
        <div className="notice notice--warn">
          The Serverless API address is not configured. Set <code>PORTAL_SERVERLESS_API_URL</code>{" "}
          to enable this offering.
        </div>
      </div>
    );
  }

  if (!activeGroup) {
    return (
      <div className="page">
        <div className="page__header">
          <h1 className="page__title">⚡ Serverless</h1>
        </div>
        <div className="notice notice--warn">
          You have no group membership, so there are no workloads to show.
        </div>
      </div>
    );
  }

  let info: PlatformInfo | null = null;
  let workloads: WorkloadSummary[] = [];
  let error: string | null = null;
  try {
    [info, workloads] = await Promise.all([
      getPlatformInfo().catch(() => null),
      listWorkloads(activeGroup, session?.accessToken),
    ]);
  } catch (err) {
    error =
      err instanceof ServerlessApiError
        ? err.message
        : `Unexpected error: ${(err as Error).message}`;
  }

  return (
    <div className="page">
      <div className="page__header">
        <h1 className="page__title">⚡ Serverless</h1>
        <p className="page__subtitle">
          Functions and containers for group <strong>{activeGroup}</strong>.
        </p>
      </div>

      {info && (
        <div className="chips">
          <span className="chip">API v{info.version}</span>
          <span className="chip">Sites: {info.sites.join(", ") || "—"}</span>
          <span className="chip">Runtimes: {info.runtimes.join(", ") || "—"}</span>
          <span className="chip">Domain: {info.routeDomain}</span>
        </div>
      )}

      {error ? (
        <div className="notice notice--error">{error}</div>
      ) : workloads.length === 0 ? (
        <div className="notice">
          No workloads in <strong>{activeGroup}</strong> yet.
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Status</th>
                <th>Host</th>
                <th>Size</th>
                <th>Sites</th>
              </tr>
            </thead>
            <tbody>
              {workloads.map((w) => (
                <tr key={`${w.type}/${w.name}`}>
                  <td className="table__name">{w.name}</td>
                  <td>
                    <span className="tag">{w.type}</span>
                  </td>
                  <td>
                    <StatusPill status={w.overallStatus} />
                  </td>
                  <td>
                    <a href={`https://${w.hostname}`} target="_blank" rel="noreferrer">
                      {w.hostname}
                    </a>
                  </td>
                  <td>{w.size ?? "—"}</td>
                  <td>{w.sites.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
