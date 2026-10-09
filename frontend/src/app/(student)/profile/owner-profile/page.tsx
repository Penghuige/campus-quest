import type { Metadata } from "next";
import Link from "next/link";
import { OwnerProfileView } from "@/features/innovation/OwnerProfileView";

export const metadata: Metadata = { title: "负责人资料 · CampusQuest", description: "准备本人创新创业项目负责人资料" };

export default function OwnerProfilePage() {
  return <>
    <div className="page-head">
      <Link className="section-link" href="/profile">返回我的档案</Link>
      <h1 className="page-title">负责人资料</h1>
      <p className="page-subtitle">本人填写、仅自己可见。资料保存与负责人资格开通是两个步骤。</p>
    </div>
    <OwnerProfileView />
  </>;
}
