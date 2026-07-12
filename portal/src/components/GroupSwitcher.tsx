"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

interface GroupSwitcherProps {
  groups: string[];
  activeGroup: string | null;
}

/**
 * The GCP-style "project" picker, pinned top-left. Lists the SSO groups the
 * user belongs to; choosing one POSTs to /api/group (which validates membership
 * and sets the cookie) and refreshes so server components re-render scoped to
 * the new group.
 */
export default function GroupSwitcher({ groups, activeGroup }: GroupSwitcherProps) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  async function select(group: string) {
    if (group === activeGroup) {
      setOpen(false);
      return;
    }
    setPending(group);
    try {
      const res = await fetch("/api/group", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group }),
      });
      if (res.ok) {
        setOpen(false);
        router.refresh();
      }
    } finally {
      setPending(null);
    }
  }

  if (groups.length === 0) {
    return <span className="switcher switcher--empty">No group membership</span>;
  }

  return (
    <div className="switcher" ref={ref}>
      <button
        className="switcher__button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="switcher__label">Group</span>
        <span className="switcher__value">{activeGroup ?? "Select"}</span>
        <span className="switcher__caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <ul className="menu switcher__menu" role="listbox">
          {groups.map((g) => (
            <li key={g} role="option" aria-selected={g === activeGroup}>
              <button
                className={`menu__item ${g === activeGroup ? "menu__item--active" : ""}`}
                onClick={() => select(g)}
                disabled={pending !== null}
              >
                <span className="menu__check" aria-hidden="true">
                  {g === activeGroup ? "✓" : ""}
                </span>
                {g}
                {pending === g && <span className="menu__spinner" aria-hidden="true" />}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
