import { redirect } from "next/navigation";

import { auth, signIn } from "@/auth";
import { branding } from "@/lib/config";

/**
 * Sign-in landing. If already authenticated it bounces to the console; otherwise
 * it presents a single "Sign in with SSO" action that starts the Keycloak
 * Authorization Code + PKCE flow. No local accounts - identity is SSO only.
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string }>;
}) {
  const session = await auth();
  const { callbackUrl } = await searchParams;
  if (session) redirect(callbackUrl || "/dashboard");

  return (
    <main className="login">
      <div className="login__card">
        <div className="login__logo" aria-hidden="true">
          ☁️
        </div>
        <h1 className="login__title">{branding.productName}</h1>
        <p className="login__subtitle">{branding.organization} platform console</p>
        <form
          action={async () => {
            "use server";
            await signIn("keycloak", { redirectTo: callbackUrl || "/dashboard" });
          }}
        >
          <button className="btn btn--primary login__button" type="submit">
            Sign in with SSO
          </button>
        </form>
        <p className="login__hint">You will be redirected to your organization&rsquo;s sign-in.</p>
      </div>
    </main>
  );
}
