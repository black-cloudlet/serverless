/**
 * SSO group-name handling, ported from the Serverless API so the portal and the
 * downstream APIs agree on group identity.
 *
 * The Serverless API normalizes every group at its edge (see
 * `api/models/common.py::normalize_group`): it strips the Keycloak path prefix
 * ("/") and a leading `ggd-<1-4 digits>-` prefix, so "/ggd-1234-platforms" and
 * "platforms" name the same group. The portal normalizes identically so the
 * group it shows in the switcher and sends as the `group` query param matches
 * exactly what the API authorizes against.
 */

// Leading "ggd-<1-4 digits>-" prefix some OIDC groups carry
// (e.g. "ggd-1234-platforms" is the group "platforms").
const GGD_PREFIX = /^ggd-\d{1,4}-/;

/**
 * Normalize a group name to its bare, canonical form.
 *
 * Mirrors the Python `normalize_group`: `lstrip("/")` then strip the
 * `ggd-<digits>-` prefix.
 */
export function normalizeGroup(group: string): string {
  return group.replace(/^\/+/, "").replace(GGD_PREFIX, "");
}

/**
 * Normalize a list of raw OIDC groups, dropping empties and de-duplicating
 * (normalization can collapse "/ggd-1-team" and "team" onto the same name).
 * The result is sorted for a stable order in the group switcher.
 */
export function normalizeGroups(raw: unknown): string[] {
  const list = Array.isArray(raw) ? raw : typeof raw === "string" ? [raw] : [];
  const normalized = list
    .filter((g): g is string => typeof g === "string" && g.length > 0)
    .map(normalizeGroup)
    .filter((g) => g.length > 0);
  return Array.from(new Set(normalized)).sort();
}
