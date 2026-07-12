/** Liveness/readiness probe target. Public, static, no auth or downstream calls. */
export const dynamic = "force-static";

export function GET() {
  return Response.json({ status: "ok" });
}
