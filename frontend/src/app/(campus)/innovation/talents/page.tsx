import type { Metadata } from 'next';
import Link from 'next/link';
import { PublicTalentsView } from '@/features/innovation/PublicTalentsView';
export const metadata:Metadata={title:'人才库 · 华师令'};
export default async function TalentsPage({searchParams}:{searchParams:Promise<Record<string,string|string[]|undefined>>}){const values=await searchParams;const query=new URLSearchParams();for(const [key,value] of Object.entries(values)){if(Array.isArray(value))for(const item of value)query.append(key,item);else if(value!==undefined)query.set(key,value);}return <><div className="page-head"><Link className="section-link" href="/innovation">返回创新创业</Link><h1 className="page-title">人才库</h1><p className="page-subtitle">按能力与合作意向寻找同学</p><p className="field-hint">演示资料为模拟内容；仅展示同学本人确认公开的档案。</p></div><PublicTalentsView query={query.toString()}/></>;}
