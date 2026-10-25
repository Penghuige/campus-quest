import Link from "next/link";
import { PublicAchievementsView } from "@/features/innovation/PublicAchievementsView";
export default async function AchievementPage({ params }: { params: Promise<{ achievementId: string }> }) { const { achievementId } = await params; return <><Link className="section-link" href="/innovation/achievements">返回校内成果</Link><PublicAchievementsView id={achievementId} /></>; }
