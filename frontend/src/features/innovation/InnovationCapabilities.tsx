"use client";

import { Button } from "@/components/ui/button";
import { SectionError, SectionSkeleton } from "@/components/ui/sectionStates";
import { useSection } from "@/components/ui/useSection";
import { useSession } from "@/features/auth/session";
import { getAuthEpoch } from "@/lib/accessToken";
import { getInnovationCapabilities } from "./operationsApi";

export function InnovationCapabilities() {
  const { state } = useSession();
  if (state.status !== "authenticated" || state.me.role !== "STUDENT" || state.me.status !== "ACTIVE") return null;
  return <Capabilities key={`${state.me.id}:${getAuthEpoch()}`} />;
}

function Capabilities() {
  const { state, retry } = useSection(getInnovationCapabilities);
  return <section className="section" aria-label="我的双创身份">
    <h2 className="section-title">我的双创身份</h2>
    {state.status === "loading" ? <SectionSkeleton /> : state.status === "error" ? <SectionError error={state.error} onRetry={retry} /> : <>
      <p role="status">{state.data.operations_enabled ? "已获双创运营授权" : "尚未获双创运营授权"}</p>
      <p className="field-hint">{state.data.operations_enabled ? "由管理员指定。成果审核、内容维护和举报处理功能正在建设中；此身份不包含积分调整权限。" : "双创运营学生由管理员指定。负责人资料和项目草稿不需要运营身份。"}</p>
      <Button variant="secondary" onClick={retry}>重新读取身份</Button>
    </>}
  </section>;
}
