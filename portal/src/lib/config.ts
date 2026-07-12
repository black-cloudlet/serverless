/**
 * Environment-driven portal configuration (12-factor), mirroring how the
 * Serverless API is configured (`PORTAL_*` here vs `SERVERLESS_*` there).
 *
 * In production these values are projected from Vault via the External Secrets
 * Operator and the Helm chart's ConfigMap (see charts/portal). Nothing dynamic
 * is hardcoded; every address and SSO parameter is an env var.
 */

function env(name: string, fallback = ""): string {
  return process.env[name] ?? fallback;
}

function envList(name: string): string[] {
  const raw = process.env[name];
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed.map(String);
  } catch {
    // Fall back to a comma-separated list for operator convenience.
    return raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return [];
}

/** SSO / Keycloak (RHBK) OIDC settings shared with the downstream APIs. */
export interface OidcConfig {
  /** Realm issuer, e.g. https://sso.internal/realms/serverless. */
  issuer: string;
  /** Confidential client id the portal logs in as (Auth Code + PKCE). */
  clientId: string;
  /** Client secret (from Vault via ESO in prod). */
  clientSecret: string;
  /** Token claim carrying the user's groups (default "groups"). */
  groupsClaim: string;
  /** Normalized groups that grant the platform-admin role. */
  adminGroups: string[];
  /** Optional end-session cleanup on sign-out. */
  postLogoutRedirect: string;
}

export const oidc: OidcConfig = {
  issuer: env("PORTAL_OIDC_ISSUER", "https://sso.internal/realms/serverless"),
  clientId: env("PORTAL_OIDC_CLIENT_ID", "cloud-console-portal"),
  clientSecret: env("PORTAL_OIDC_CLIENT_SECRET"),
  groupsClaim: env("PORTAL_OIDC_GROUPS_CLAIM", "groups"),
  adminGroups: envList("PORTAL_ADMIN_GROUPS"),
  postLogoutRedirect: env("PORTAL_POST_LOGOUT_REDIRECT"),
};

/** Branding shown in the top bar and browser tab. */
export const branding = {
  productName: env("PORTAL_PRODUCT_NAME", "Cloud Console"),
  organization: env("PORTAL_ORG_NAME", "Platform"),
};

/**
 * Whether a caller with these normalized groups holds the platform-admin role.
 * Same rule as the Serverless API (`principal_from_claims`): admin iff any of
 * the caller's groups is in the configured admin groups.
 */
export function isAdmin(groups: string[]): boolean {
  return groups.some((g) => oidc.adminGroups.includes(g));
}
