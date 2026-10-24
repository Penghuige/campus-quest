import type { Metadata } from 'next';
import Link from 'next/link';
import { TalentProfileView } from '@/features/innovation/TalentProfileView';
export const metadata:Metadata={title:'我的人才档案 · 华师令'};
export default function TalentProfilePage(){return <><div className="page-head"><Link className="section-link" href="/profile">返回我的档案</Link><h1 className="page-title">我的人才档案</h1><p className="page-subtitle">介绍你的优势，寻找合适的项目与合作机会</p><p className="field-hint">演示阶段：使用预置账号与明确模拟资料，学校统一身份认证尚未接入。</p></div><TalentProfileView/></>;}
