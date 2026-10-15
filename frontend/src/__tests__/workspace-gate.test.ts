/**
 * PR #4 hardening Task 3: role-to-workspace routing — the four
 * owner-named regressions against the PURE decision module the shells
 * and login forms consume (`features/auth/workspace.ts`), plus the
 * admin shell's gate (Plan 09 Task 10):
 *
 * 1. student login lands on the student home;
 * 2. each staff role lands on ITS OWN workspace — teachers on the
 *    review queue, admins on the admin workspace (the owner report
 *    2026-10-15: an ADMIN login/refresher landing on /teacher/reviews
 *    read as being taken to the wrong cockpit);
 * 3. a staff session opening "/" gets staff-guidance (the student shell
 *    renders ONLY the guidance — `kind: "student"` is the sole branch
 *    that mounts the student pages, so no student-only API storm can
 *    fire), and the guidance carries EACH ROLE's OWN workspace link;
 * 4. a student session opening /teacher/* gets student-guidance with the
 *    student entry link.
 * 5. (Task 10 step 1) /admin/* mounts ONLY for ADMIN: TEACHER and
 *    STUDENT sessions get guidance branches — the shell renders only
 *    the guidance panel, so ZERO admin API calls can fire (the
 *    privilege-navigation contract the e2e spec pins end-to-end).
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  ADMIN_LANDING_PATH,
  adminWorkspaceGate,
  landingPathForRole,
  STUDENT_LANDING_PATH,
  studentWorkspaceGate,
  teacherWorkspaceGate,
  TEACHER_LANDING_PATH,
} from "../features/auth/workspace";

describe("login landing by role", () => {
  test("regression 1: a student login lands on the student home", () => {
    assert.equal(landingPathForRole("STUDENT"), "/");
    assert.equal(STUDENT_LANDING_PATH, "/");
  });

  test("regression 2 (owner report 2026-10-15): each staff role lands on ITS OWN workspace", () => {
    assert.equal(landingPathForRole("TEACHER"), "/teacher/reviews");
    assert.equal(TEACHER_LANDING_PATH, "/teacher/reviews");
    assert.equal(landingPathForRole("ADMIN"), "/admin/users");
    assert.equal(ADMIN_LANDING_PATH, "/admin/users");
  });
});

describe("student workspace gate (the / student shell's branch)", () => {
  test("a STUDENT session is the only kind that may mount the workspace", () => {
    assert.deepEqual(studentWorkspaceGate("STUDENT"), { kind: "student" });
  });

  test("regression 3: TEACHER/ADMIN on \"/\" get staff-guidance with EACH ROLE's own workspace link — never the pages", () => {
    const expectations = {
      TEACHER: "/teacher/reviews",
      ADMIN: "/admin/users",
    } as const;
    for (const role of ["TEACHER", "ADMIN"] as const) {
      const gate = studentWorkspaceGate(role);
      assert.equal(gate.kind, "staff-guidance");
      if (gate.kind === "staff-guidance") {
        assert.equal(gate.workspacePath, expectations[role]);
      }
    }
  });
});

describe("teacher workspace gate (the /teacher shell's branch)", () => {
  test("TEACHER/ADMIN pass (the workspace mounts)", () => {
    assert.deepEqual(teacherWorkspaceGate("TEACHER"), { kind: "staff" });
    assert.deepEqual(teacherWorkspaceGate("ADMIN"), { kind: "staff" });
  });

  test("regression 4: a STUDENT on /teacher/* gets student-guidance with the student link", () => {
    const gate = teacherWorkspaceGate("STUDENT");
    assert.equal(gate.kind, "student-guidance");
    if (gate.kind === "student-guidance") {
      assert.equal(gate.workspacePath, "/");
    }
  });
});

describe("admin workspace gate (the /admin shell's branch; Task 10 step 1)", () => {
  test("regression 5: ADMIN is the ONLY role that may mount /admin/*", () => {
    assert.deepEqual(adminWorkspaceGate("ADMIN"), { kind: "admin" });
  });

  test("a TEACHER session gets staff-guidance carrying the teacher workspace link", () => {
    const gate = adminWorkspaceGate("TEACHER");
    assert.equal(gate.kind, "staff-guidance");
    if (gate.kind === "staff-guidance") {
      assert.equal(gate.workspacePath, "/teacher/reviews");
    }
  });

  test("a STUDENT session gets student-guidance carrying the student entry link", () => {
    const gate = adminWorkspaceGate("STUDENT");
    assert.equal(gate.kind, "student-guidance");
    if (gate.kind === "student-guidance") {
      assert.equal(gate.workspacePath, "/");
    }
  });
});
