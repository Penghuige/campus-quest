import type { Metadata } from "next";
import { OwnerQualificationAdmin } from "@/features/innovation/OwnerQualificationAdmin";

export const metadata: Metadata = { title: "负责人资格 · CampusQuest 管理后台" };

export default function OwnerQualificationsPage() {
  return <>
    <div className="page-head">
      <h1 className="page-title">负责人资格</h1>
      <p className="page-subtitle">查看学生已提交的四项资料快照，由管理员人工开通负责人资格。</p>
    </div>
    <OwnerQualificationAdmin />
  </>;
}
