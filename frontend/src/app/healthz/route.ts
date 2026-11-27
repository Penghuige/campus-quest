/**
 * Web liveness endpoint for deployment health checks.
 *
 * The platform probes a module-defined path to decide whether a web
 * candidate is serving. That probe must never point at a business page:
 * the root redirects anonymous visitors to the login screen (307), and a
 * health check should not encode UI routing as liveness.
 *
 * This route is static, unauthenticated and dependency-free: it proves
 * the Next process is serving requests, nothing about the API or the
 * database. With `basePath=/campus` it is reachable at `/campus/healthz`.
 */
export const dynamic = "force-static";

export function GET(): Response {
  return Response.json({ status: "ok" });
}