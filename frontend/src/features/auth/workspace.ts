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

/** Where a successful STAFF (teacher/admin) login lands: the review queue. */
export const TEACHER_LANDING_PATH = "/teacher/reviews";

/** Landing path by the session's role (login-success redirect target). */
export function landingPathForRole(role: Role): string {
  return role === "STUDENT" ? STUDENT_LANDING_PATH : TEACHER_LANDING_PATH;
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
  return { kind: "staff-guidance", workspacePath: TEACHER_LANDING_PATH };
}

/**
 * The teacher shell's gate: TEACHER/ADMIN pass; a STUDENT session gets
 * `student-guidance` carrying the student entry path (permission
 * guidance, not a page of 403ing sections). ACTIVE-account and
 * confirmed-TOTP enforcement stays with the backend's staff guard — a
 * 403 surfaces through each section's error state.
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
