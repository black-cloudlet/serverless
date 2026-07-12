"use client";

import Link from "next/link";
import { signOut } from "next-auth/react";
import { useEffect, useRef, useState } from "react";

interface ProfileMenuProps {
  user: { name: string; username: string; email: string; isAdmin: boolean };
  activeGroup: string | null;
  groups: string[];
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/**
 * The right-hand profile control: an avatar that opens a panel with the user's
 * name/username/email, their SSO group membership (with the active one marked),
 * the admin badge, and sign-out.
 */
export default function ProfileMenu({ user, activeGroup, groups }: ProfileMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  return (
    <div className="profile" ref={ref}>
      <button
        className="profile__avatar"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={user.name}
      >
        {initials(user.name || user.username)}
      </button>
      {open && (
        <div className="menu profile__panel" role="dialog" aria-label="Account">
          <div className="profile__header">
            <div className="profile__avatar profile__avatar--lg">
              {initials(user.name || user.username)}
            </div>
            <div className="profile__id">
              <div className="profile__name">{user.name || user.username}</div>
              {user.email && <div className="profile__email">{user.email}</div>}
              <div className="profile__username">@{user.username}</div>
              {user.isAdmin && <span className="badge badge--admin">Platform admin</span>}
            </div>
          </div>

          <div className="profile__section">
            <div className="profile__section-title">Groups</div>
            <ul className="profile__groups">
              {groups.length === 0 && (
                <li className="profile__group profile__group--empty">None</li>
              )}
              {groups.map((g) => (
                <li
                  key={g}
                  className={`profile__group ${g === activeGroup ? "profile__group--active" : ""}`}
                >
                  <span className="profile__group-dot" aria-hidden="true" />
                  {g}
                  {g === activeGroup && <span className="badge badge--active">Active</span>}
                </li>
              ))}
            </ul>
          </div>

          <div className="profile__actions">
            <Link className="btn btn--ghost" href="/profile" onClick={() => setOpen(false)}>
              Manage profile
            </Link>
            <button className="btn btn--ghost" onClick={() => signOut({ callbackUrl: "/login" })}>
              Sign out
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
