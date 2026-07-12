import { redirect } from "next/navigation";

/** The root simply enters the console dashboard (auth is enforced by middleware). */
export default function Home() {
  redirect("/dashboard");
}
