import type { Metadata } from 'next';
import { PublicTalentsView } from '@/features/innovation/PublicTalentsView';
export const metadata:Metadata={title:'人才档案 · 华师令'};
export default async function TalentDetailPage({params,searchParams}:{params:Promise<{id:string}>;searchParams:Promise<Record<string,string|string[]|undefined>>}){const [{id},values]=await Promise.all([params,searchParams]);const query=new URLSearchParams();for(const [key,value] of Object.entries(values)){if(Array.isArray(value))for(const item of value)query.append(key,item);else if(value!==undefined)query.set(key,value);}return <PublicTalentsView id={id} query={query.toString()}/>;}
