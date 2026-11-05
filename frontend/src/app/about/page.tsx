import type { Metadata } from "next";
import { LandingPage } from "@/features/landing/LandingPage";
export const metadata: Metadata = { title: "智汇华师 · 校园协作与成长", description: "参与校园任务，寻找项目伙伴，展示技能与作品" };
export default function AboutPage() { return <LandingPage />; }
