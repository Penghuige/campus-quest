/**
 * Role-to-workspace routing (design §8/§10; PR #4 hardening Task 3).
 * PURE decision module: the login forms land on the role's own
 * workspace, and each shell's gate renders its workspace ONLY for the
 * role it serves — a mismatched session gets guidance to the right
 * workspace INSTEAD of a page of failing sections (and, on the student
 * side, instead of ever mounting the student-only data islands, so no
 * student-only API storm can fire from a staff session).
 */
import type { components } from "@/lib/api/schema";

type Role = components["schemas"]["Role"];

/** Where a successful STUDENT login lands (the student home). */
export const STUDENT_LANDING_PATH = "/";

/** Where a successful TEACHER login lands: the review queue. */
export const TEACHER_LANDING_PATH = "/teacher/reviews";

/**
 * Where a successful ADMIN login lands: the Wave 2a overview (spec
 * §4.1 — the dashboard IS the admin home; users stays one nav entry
 * away). Owner report (2026-10-15): each staff role lands on its OWN
 * cockpit — an admin taken to the teacher queue reads as being routed
 * into someone else's workspace.
 */
export const ADMIN_LANDING_PATH = "/admin";

/** Landing path by the session's role (login-success redirect target). */
export function landingPathForRole(role: Role): string {
  if (role === "STUDENT") {
    return STUDENT_LANDING_PATH;
  }
  if (role === "ADMIN") {
    return ADMIN_LANDING_PATH;
  }
  return TEACHER_LANDING_PATH;
}

/**
 * The student shell's gate. `kind: "student"` is the ONLY branch that
 * may render the student workspace (children included); TEACHER/ADMIN
 * get `staff-guidance` carrying the staff workspace path — the shell
 * renders the guidance panel and never mounts the student pages, so no
 * student-only request fires from a staff session.
 */
export type StudentWorkspaceGate =
  | { kind: "student" }
  | { kind: "staff-guidance"; workspacePath: string };

export function studentWorkspaceGate(role: Role): StudentWorkspaceGate {
  if (role === "STUDENT") {
    return { kind: "student" };
  }
  // Each staff role is pointed at its OWN cockpit (the owner report's
  // landing rule, mirrored in the guidance link).
  return { kind: "staff-guidance", workspacePath: landingPathForRole(role) };
}

/**
 * The teacher shell's gate: TEACHER/ADMIN pass; a STUDENT session gets
 * `student-guidance` carrying the student entry path (permission
 * guidance, not a page of 403ing sections). ACTIVE-account and
 * confirmed-TOTP enforcement stays with the backend's staff guard — a
 * 403 surfaces through each section's error state.
 *
 * Deliberate boundary (owner report 2026-10-15): admins KEEP access to
 * the teacher workspace — the fix changed only the DEFAULT LANDING
 * (`landingPathForRole`), never this gate. An admin browsing
 * /teacher/* is a legitimate cross-cockpit visit, not a misroute.
 */
export type TeacherWorkspaceGate =
  | { kind: "staff" }
  | { kind: "student-guidance"; workspacePath: string };

export function teacherWorkspaceGate(role: Role): TeacherWorkspaceGate {
  if (role === "STUDENT") {
    return { kind: "student-guidance", workspacePath: STUDENT_LANDING_PATH };
  }
  return { kind: "staff" };
}

/**
 * The admin shell's gate (Plan 09 Task 10): `kind: "admin"` is the ONLY
 * branch that may mount the /admin workspace — a TEACHER session gets
 * `staff-guidance` carrying the teacher workspace path and a STUDENT
 * session gets `student-guidance`. The shell renders ONLY the guidance
 * panel in both mismatch cases (design §10), so the six admin pages —
 * and therefore every /admin API island inside them — never mount for a
 * non-admin session: zero admin API calls fire (the plan's step-1
 * privilege test). Backend `require_admin_actor` remains the authority;
 * this gate is the no-wasted-requests UX mirror.
 */
export type AdminWorkspaceGate =
  | { kind: "admin" }
  | { kind: "staff-guidance"; workspacePath: string }
  | { kind: "student-guidance"; workspacePath: string };

export function adminWorkspaceGate(role: Role): AdminWorkspaceGate {
  if (role === "ADMIN") {
    return { kind: "admin" };
  }
  if (role === "TEACHER") {
    return { kind: "staff-guidance", workspacePath: TEACHER_LANDING_PATH };
  }
  return { kind: "student-guidance", workspacePath: STUDENT_LANDING_PATH };
}
