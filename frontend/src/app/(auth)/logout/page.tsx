import type { Metadata } from "next";
import { LogoutView } from "@/features/auth/LogoutView";

export const metadata: Metadata = { title: "退出登录 · 华师令" };
export default function LogoutPage() { return <LogoutView />; }
