"use client";
/**
 * Defect #4 (QA 2026-09-30) — the avatar section of the 个人信息
 * subpage (avatar proposal D1–D6; backend half in flight).
 *
 * CAPABILITY GATE: the section renders only while the /me payload
 * carries a boolean `has_avatar` (avatarView.avatarSupported). That
 * gate was the pre-#21 hiding mechanism — the backend now always
 * sends the flag, so it stays open; it survives as a cheap contract
 * guard (a schema regression hides the section instead of breaking
 * it).
 *
 * Display path: the memory-only bearer cannot ride a plain <img src>,
 * so the bytes come through an Authorization-headed fetch and render
 * from an object URL (revoked on replace/unmount). A failed display
 * fetch degrades to the initial-letter avatar (D5's client-side
 * fallback) — a nicety must never break the page.
 */
import { useEffect, useRef, useState, type ChangeEvent } from "react";

import { getAccessToken } from "@/lib/accessToken";
import { avatarUrl, deleteAvatar, uploadAvatar } from "./api";
import { SettingsSection } from "./AccountSettings";
import { cropToSquareFile } from "./avatarCrop";
import {
  avatarErrorText,
  avatarSupported,
  validateAvatarSize,
  validateAvatarType,
} from "./avatarView";
import { firstGraphemeCluster } from "./validation";
import type { MeDto } from "./api";
import { useSession } from "./session";

/** Fetch the avatar bytes WITH the tab bearer (see the file comment). */
async function fetchAvatarBlob(userId: string): Promise<Blob | null> {
  const token = getAccessToken();
  if (token === null) {
    return null;
  }
  const response = await fetch(avatarUrl(userId), {
    headers: { Authorization: `Bearer ${token}` },
    credentials: "include",
  });
  if (!response.ok) {
    return null;
  }
  return response.blob();
}
import { Button } from "@/components/ui/button";


export function AvatarSection() {
  const { state, refresh } = useSession();
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "upload" | "delete">(null);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  // Bumped after each successful change: replaces + deletes may keep
  // has_avatar true, and the object-URL cache must not outlive them.
  const [changeEpoch, setChangeEpoch] = useState(0);
  const deleteConfirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const authenticated = state.status === "authenticated";
  // MeDto has carried the live has_avatar flag since #21 landed.
  const me: MeDto | null = authenticated ? state.me : null;

  useEffect(() => {
    let cancelled = false;
    /** Clear any stale object URL (revoke + null), deferred a microtask
     * so the effect body stays free of synchronous setState
     * (react-hooks/set-state-in-effect — the session.ts idiom). */
    const clearObjectUrl = () => {
      Promise.resolve().then(() => {
        if (!cancelled) {
          setObjectUrl((current) => {
            if (current !== null) {
              URL.revokeObjectURL(current);
            }
            return null;
          });
        }
      });
    };

    if (me === null || me.has_avatar !== true) {
      // Avatar gone (removed, or never set): drop any STALE object URL
      // so the figure falls back to the initial letter — without this
      // the removed avatar's blob URL kept rendering (rebase-e2e find).
      clearObjectUrl();
      return () => {
        cancelled = true;
      };
    }

    let created: string | null = null;
    fetchAvatarBlob(me.id)
      .then((blob) => {
        if (cancelled) {
          return;
        }
        if (blob !== null) {
          created = URL.createObjectURL(blob);
          // Replace-and-revoke: a fresh upload supersedes the previous
          // URL without waiting for unmount.
          setObjectUrl((current) => {
            if (current !== null && current !== created) {
              URL.revokeObjectURL(current);
            }
            return created;
          });
        }
      })
      .catch(() => {
        // Display failure degrades to the initial avatar (D5).
      });
    return () => {
      cancelled = true;
      if (created !== null) {
        URL.revokeObjectURL(created);
      }
    };
    // Keyed on the IDENTITY fields, not the `me` object: every /me
    // refresh produces a new object reference, and re-fetching the
    // avatar bytes on each would waste the ETag-cached display fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me?.id, me?.has_avatar, changeEpoch]);

  if (me === null || !avatarSupported(me)) {
    // The capability gate (see the file comment) + the shell's own
    // session gate; nothing renders before the backend lands.
    return null;
  }

  async function onPick(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // re-picking the same file must re-run
    if (file === undefined) {
      return;
    }
    setError(null);
    setSavedNote(null);
    // Type gate on the raw pick (a wrong type never costs a decode);
    // the SIZE gate runs on the cropped product below — a large photo
    // legitimately shrinks under the ceiling through the crop.
    const typeIssue = validateAvatarType(file);
    if (typeIssue !== null) {
      setError(typeIssue);
      return;
    }
    setBusy("upload");
    try {
      const cropped = await cropToSquareFile(file);
      const sizeIssue = validateAvatarSize(cropped.size);
      if (sizeIssue !== null) {
        setError(sizeIssue);
        return;
      }
      // Raw-body upload (the ratified D2 revision): the cropped image
      // IS the request body — no multipart envelope anywhere.
      await uploadAvatar(cropped);
      setChangeEpoch((epoch) => epoch + 1);
      refresh();
      setSavedNote("头像已更新。");
    } catch (cause) {
      setError(avatarErrorText(cause));
    } finally {
      setBusy(null);
    }
  }

  async function onDelete() {
    if (!confirmingDelete) {
      // Two-step inline confirm (no native dialogs): the button itself
      // becomes the confirmation for a few seconds.
      setConfirmingDelete(true);
      deleteConfirmTimer.current = setTimeout(() => setConfirmingDelete(false), 4000);
      return;
    }
    if (deleteConfirmTimer.current !== null) {
      clearTimeout(deleteConfirmTimer.current);
    }
    setConfirmingDelete(false);
    setError(null);
    setSavedNote(null);
    setBusy("delete");
    try {
      await deleteAvatar();
      setChangeEpoch((epoch) => epoch + 1);
      refresh();
      setSavedNote("已恢复默认头像。");
    } catch (cause) {
      setError(avatarErrorText(cause));
    } finally {
      setBusy(null);
    }
  }

  return (
    <SettingsSection title="头像" hint="支持 PNG、JPEG、WebP，最大 2 MB；上传前自动裁剪为方形，每 10 分钟可修改一次。">
      <div className="avatar-row">
        {objectUrl !== null ? (
          <span className="avatar-figure">
            {/* A runtime blob, not a bundler asset — next/image has no
                object-URL source; sized explicitly below. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={objectUrl} alt="当前头像" width={64} height={64} />
          </span>
        ) : (
          <span className="avatar-figure avatar-figure-initial" aria-hidden="true">
            {firstGraphemeCluster(me.nickname, "同")}
          </span>
        )}
        <div className="avatar-actions">
          <label className="btn btn-secondary">
            {busy === "upload" ? "上传中…" : "选择图片并更新"}
            <input
              className="avatar-file-input"
              type="file"
              accept="image/png,image/jpeg,image/webp"
              disabled={busy !== null}
              onChange={onPick}
            />
          </label>
          {me.has_avatar === true ? (
            <Button
              variant="secondary"
              onClick={onDelete}
              disabled={busy !== null}
            >
              {busy === "delete" ? "移除中…" : confirmingDelete ? "确认移除？" : "移除头像"}
            </Button>
          ) : null}
        </div>
      </div>
      {error !== null ? (
        <p className="field-error" role="alert">
          {error}
        </p>
      ) : null}
      {savedNote !== null ? (
        <p className="field-hint" role="status">
          {savedNote}
        </p>
      ) : null}
    </SettingsSection>
  );
}
