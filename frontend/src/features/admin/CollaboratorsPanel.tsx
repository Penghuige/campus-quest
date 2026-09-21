"use client";
/**
 * Collaborator management (spec §4.2; brief step 1): grant a Teacher an
 * explicit capability set, update it, or remove the grant — each action
 * is a direct wire call whose outcome renders from the server echo.
 *
 * WIRE-CONTRACT NOTE (why no full roster): the backend exposes only
 * PUT/DELETE on /teacher/tasks/{id}/collaborators/{teacher_id} — there
 * is NO list endpoint, and `TeacherTaskResponse` carries no collaborator
 * rows. So the roster below is session-local: rows appear from THIS
 * session's PUT echoes (the canonical granted set, storage order) and
 * persist until reload; removal by id works for ANY grant through the
 * remove action next to the id input. When a GET lands, replace the
 * local rows with the fetched page (the api seam is already in place).
 *
 * Permission rules are the server's (grant-within-own-set, target must
 * be a Teacher, duplicates are 409 VALIDATION_ERROR): the client sends
 * the checked set and renders the typed envelope, it never pre-judges
 * standing.
 */
import { useState } from "react";

import { SectionError } from "@/components/ui/sectionStates";

import {
  addCollaborator,
  removeCollaborator,
  type CollaboratorDto,
} from "./teacherApi";
import { COLLABORATOR_PERMISSIONS, permissionLabels } from "./teacherView";

export function CollaboratorsPanel({ taskId }: { taskId: string }) {
  const [teacherId, setTeacherId] = useState("");
  const [permissions, setPermissions] = useState<string[]>(["VIEW_TASK"]);
  const [idError, setIdError] = useState<string | null>(null);
  const [permError, setPermError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  /** Session grants keyed by teacher id (PUT echoes; see the docstring). */
  const [grants, setGrants] = useState<CollaboratorDto[]>([]);

  function togglePermission(code: string) {
    setPermissions((previous) =>
      previous.includes(code)
        ? previous.filter((item) => item !== code)
        : [...previous, code],
    );
    if (permError !== null) {
      setPermError(null);
    }
  }

  async function onAdd() {
    if (busy) {
      return;
    }
    const trimmed = teacherId.trim();
    setIdError(uuidShapeOk(trimmed) ? null : "请输入协作者的教师账号 ID（UUID）");
    setPermError(permissions.length > 0 ? null : "权限集合不能为空");
    setError(null);
    if (trimmed.length === 0 || !uuidShapeOk(trimmed) || permissions.length === 0) {
      return;
    }
    setBusy(true);
    try {
      const granted = await addCollaborator(taskId, trimmed, permissions);
      setGrants((previous) => [
        ...previous.filter((row) => row.teacher_id !== granted.teacher_id),
        granted,
      ]);
      setTeacherId("");
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  async function onRemove(targetId: string) {
    if (busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await removeCollaborator(taskId, targetId);
      setGrants((previous) => previous.filter((row) => row.teacher_id !== targetId));
      if (teacherId.trim() === targetId) {
        // A remove-by-id for a row not granted this session still clears
        // the input on success so the operator sees the call landed.
        setTeacherId("");
      }
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section" aria-label="协作者管理">
      <div className="section-head">
        <h3 className="section-title">协作者管理</h3>
      </div>
      <p className="field-hint">
        为其他教师授予本任务的明确权限；协作者不能授予超出自身拥有的权限。已授权但未在本页显示的协作者，可在下方输入其
        ID 后使用「移除权限」。
      </p>

      <div className="field">
        <label className="field-label" htmlFor="collaborator-id">
          教师账号 ID（UUID）
        </label>
        <input
          id="collaborator-id"
          className="input mono"
          value={teacherId}
          onChange={(event) => {
            setTeacherId(event.target.value);
            if (idError !== null) {
              setIdError(null);
            }
          }}
          placeholder="00000000-0000-0000-0000-000000000000"
          aria-invalid={idError !== null}
          disabled={busy}
        />
        {idError !== null ? <p className="field-error">{idError}</p> : null}
      </div>

      <fieldset className="field">
        <legend className="field-label">授予权限</legend>
        <div className="perm-checks" role="group" aria-label="授予权限">
          {COLLABORATOR_PERMISSIONS.map((option) => (
            <label key={option.value} className="identity-option" title={option.hint}>
              <input
                type="checkbox"
                checked={permissions.includes(option.value)}
                onChange={() => togglePermission(option.value)}
                disabled={busy}
              />
              <span>{option.label}</span>
            </label>
          ))}
        </div>
        {permError !== null ? <p className="field-error">{permError}</p> : null}
      </fieldset>

      <div className="dialog-actions">
        <button type="button" className="btn btn-primary" onClick={() => void onAdd()} disabled={busy} aria-busy={busy}>
          {busy ? <span className="spinner" aria-hidden="true" /> : null}
          <span>添加 / 更新权限</span>
        </button>
        <button
          type="button"
          className="btn btn-danger"
          onClick={() => {
            const trimmed = teacherId.trim();
            setIdError(uuidShapeOk(trimmed) ? null : "请输入要移除的教师账号 ID（UUID）");
            if (uuidShapeOk(trimmed)) {
              void onRemove(trimmed);
            }
          }}
          disabled={busy}
        >
          移除权限
        </button>
      </div>

      {grants.length > 0 ? (
        <>
          <h4 className="report-group-title">本次会话已授予（{grants.length}）</h4>
          <ul className="collab-list">
            {grants.map((grant) => (
              <li key={grant.teacher_id} className="collab-row">
                <span className="mono collab-id">{grant.teacher_id}</span>
                <span className="collab-perms">
                  {permissionLabels(grant.permissions).join("、")}
                </span>
                <button
                  type="button"
                  className="btn btn-ghost comment-action"
                  onClick={() => void onRemove(grant.teacher_id)}
                  disabled={busy}
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {error !== null ? <SectionError error={error} /> : null}
    </section>
  );
}

/** Shape-only check (8-4-4-4-12 hex): existence/role is the server's verdict. */
function uuidShapeOk(value: string): boolean {
  return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(
    value,
  );
}
