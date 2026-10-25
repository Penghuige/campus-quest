import type { Metadata } from "next";
import { OperationsGrantAdmin } from "@/features/innovation/OperationsGrantAdmin";

export const metadata: Metadata = { title: "双创运营授权 · CampusQuest 管理后台" };

export default function InnovationOperationsPage() {
  return <>
    <div className="page-head">
      <h1 className="page-title">双创运营授权</h1>
      <p className="page-subtitle">由管理员指定和撤回运营学生，所有操作记入审计日志。</p>
    </div>
    <OperationsGrantAdmin />
  </>;
}
