/**
 * Set the active group (the top-bar project picker). Accepts a POST with
 * `{ group }`, validates it against the caller's SSO groups, and stores it in
 * the cookie read by `resolveActiveGroup`. A group the user is not a member of
 * is rejected - the cookie can never grant access the token doesn't.
 */

import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { GROUP_COOKIE } from "@/lib/session-group";

export async function POST(req: Request) {
  const session = await auth();
  if (!session) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  let group: unknown;
  try {
    ({ group } = await req.json());
  } catch {
    return NextResponse.json({ error: "invalid body" }, { status: 400 });
  }

  if (typeof group !== "string" || !session.user.groups.includes(group)) {
    return NextResponse.json({ error: "not a member of that group" }, { status: 403 });
  }

  const res = NextResponse.json({ group });
  res.cookies.set(GROUP_COOKIE, group, {
    httpOnly: true,
    sameSite: "lax",
    secure: true,
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
  return res;
}
