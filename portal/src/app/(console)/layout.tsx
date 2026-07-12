import { redirect } from "next/navigation";

import TopBar from "@/components/TopBar";
import SideNav from "@/components/SideNav";
import { auth } from "@/auth";
import { branding } from "@/lib/config";
import { getServicesByCategory } from "@/lib/services";
import { resolveActiveGroup } from "@/lib/session-group";

/**
 * The console shell (GCP-like): a fixed top bar with the product name, the
 * group/project switcher on the left and the profile menu on the right, and a
 * left navigation rail listing the platform offerings. All console pages render
 * inside `<main>`.
 */
export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  if (!session) redirect("/login");

  const groups = session.user.groups;
  const activeGroup = await resolveActiveGroup(groups);
  const categories = getServicesByCategory();

  return (
    <div className="shell">
      <TopBar
        productName={branding.productName}
        organization={branding.organization}
        user={{
          name: session.user.name ?? session.user.username,
          username: session.user.username,
          email: session.user.email ?? "",
          isAdmin: session.user.isAdmin,
        }}
        groups={groups}
        activeGroup={activeGroup}
      />
      <div className="shell__body">
        <SideNav categories={categories} />
        <main className="shell__main">{children}</main>
      </div>
    </div>
  );
}
