import { auth } from "@/auth";
import { resolveActiveGroup } from "@/lib/session-group";

export const metadata = { title: "Profile" };

/** Full account detail: identity from the SSO token plus group membership. */
export default async function ProfilePage() {
  const session = await auth();
  const user = session?.user;
  const groups = user?.groups ?? [];
  const activeGroup = await resolveActiveGroup(groups);

  return (
    <div className="page">
      <div className="page__header">
        <h1 className="page__title">Profile</h1>
        <p className="page__subtitle">Your identity and access, as provided by SSO.</p>
      </div>

      <div className="card card--static profile-detail">
        <dl className="kv">
          <dt>Name</dt>
          <dd>{user?.name || "—"}</dd>
          <dt>Username</dt>
          <dd>@{user?.username}</dd>
          <dt>Email</dt>
          <dd>{user?.email || "—"}</dd>
          <dt>Role</dt>
          <dd>{user?.isAdmin ? "Platform admin" : "Member"}</dd>
          <dt>Active group</dt>
          <dd>{activeGroup ?? "—"}</dd>
        </dl>
      </div>

      <section>
        <h2 className="section-title">Group membership</h2>
        {groups.length === 0 ? (
          <div className="notice notice--warn">
            Your account has no group membership. Ask an administrator to add you to a group.
          </div>
        ) : (
          <ul className="grouplist">
            {groups.map((g) => (
              <li
                key={g}
                className={`grouplist__item ${g === activeGroup ? "grouplist__item--active" : ""}`}
              >
                <span className="profile__group-dot" aria-hidden="true" />
                {g}
                {g === activeGroup && <span className="badge badge--active">Active</span>}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
