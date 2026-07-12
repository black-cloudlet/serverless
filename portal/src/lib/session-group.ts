/**
 * The "active group" selection (the GCP-style project picker in the top bar).
 *
 * A user can belong to several SSO groups; the console always operates in the
 * context of exactly one, chosen in the top-left switcher. The choice is kept
 * in a cookie so it survives navigation and server-rendered requests, and is
 * always validated against the user's actual group membership - a stale or
 * forged cookie can never widen access beyond what the SSO token grants.
 */

import { cookies } from "next/headers";

export const GROUP_COOKIE = "portal_active_group";

/**
 * Resolve the active group for this request from the cookie, constrained to the
 * caller's groups. Falls back to the first group when the cookie is absent or
 * no longer valid. Returns null only when the user has no groups at all.
 */
export async function resolveActiveGroup(userGroups: string[]): Promise<string | null> {
  if (userGroups.length === 0) return null;
  const store = await cookies();
  const selected = store.get(GROUP_COOKIE)?.value;
  if (selected && userGroups.includes(selected)) return selected;
  return userGroups[0];
}
