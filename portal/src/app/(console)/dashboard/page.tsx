import Link from "next/link";

import { auth } from "@/auth";
import { getServices } from "@/lib/services";
import { resolveActiveGroup } from "@/lib/session-group";

export const metadata = { title: "Home" };

/**
 * The console home: a welcome banner scoped to the active group and a grid of
 * service cards (GCP-dashboard style). Enabled services link into their page;
 * others show as coming soon.
 */
export default async function DashboardPage() {
  const session = await auth();
  const groups = session?.user.groups ?? [];
  const activeGroup = await resolveActiveGroup(groups);
  const services = getServices();

  return (
    <div className="page">
      <div className="page__header">
        <h1 className="page__title">Welcome{session?.user.name ? `, ${session.user.name}` : ""}</h1>
        <p className="page__subtitle">
          {activeGroup ? (
            <>
              You are working in group <strong>{activeGroup}</strong>. Switch groups from the picker
              in the top bar.
            </>
          ) : (
            <>
              Your account has no group membership yet. Ask an administrator to add you to a group.
            </>
          )}
        </p>
      </div>

      <section>
        <h2 className="section-title">Services</h2>
        <div className="card-grid">
          {services.map((svc) => {
            const card = (
              <>
                <div className="card__icon" aria-hidden="true">
                  {svc.icon}
                </div>
                <div className="card__body">
                  <div className="card__title">
                    {svc.name}
                    {!svc.enabled && <span className="tag tag--soon">Coming soon</span>}
                  </div>
                  <p className="card__desc">{svc.description}</p>
                </div>
              </>
            );
            return svc.enabled ? (
              <Link key={svc.id} href={`/${svc.id}`} className="card card--link">
                {card}
              </Link>
            ) : (
              <div key={svc.id} className="card card--disabled">
                {card}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
