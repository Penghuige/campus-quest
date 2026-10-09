import type { ReactNode } from "react";
import { CampusAchievementShell } from "@/features/innovation/PublicAchievementsView";
export default function CampusLayout({ children }: { children: ReactNode }) { return <CampusAchievementShell>{children}</CampusAchievementShell>; }
