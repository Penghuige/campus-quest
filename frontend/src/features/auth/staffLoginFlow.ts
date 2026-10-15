/**
 * Staff post-login landing orchestration (owner report 2026-10-15) —
 * the testable seam between the staff login form and the routing
 * decision. The staff form serves TWO roles, so the landing target is
 * only knowable from the session's own post-login answer (readMe) —
 * never from the pre-login hook state (possibly anonymous, possibly a
 * previous account's).
 *
 * Dependency-injected (login/read/router) so the three-role landing
 * contract is unit-pinnable without a component harness; the form's
 * submit body reduces to one call through this seam.
 */
import { landingPathForRole } from "./workspace";

export interface StaffLoginLandingDeps {
  /** The completed login call (already adopted by the token manager). */
  login: () => Promise<unknown>;
  /** The session hook's refresh (invalidates the pre-login cache). */
  refreshSession: () => void;
  /** The router slice the landing uses. */
  router: {
    replace: (path: string) => void;
    refresh: () => void;
  };
  /** One-shot /me read (the role source). */
  readMe: () => Promise<{ role: string }>;
}

export async function landAfterStaffLogin(deps: StaffLoginLandingDeps): Promise<void> {
  await deps.login();
  deps.refreshSession();
  const me = await deps.readMe();
  deps.router.replace(landingPathForRole(me.role as Parameters<typeof landingPathForRole>[0]));
  deps.router.refresh();
}
