import { redirect } from "next/navigation";

/**
 * Staff login retired with the unified page (spec 2026-10-19 §4):
 * /login serves every audience now — this route redirects. The
 * invitation accept flow (/staff/invite/{token}) stays its own route.
 */
export default function StaffLoginPage() {
  redirect("/login");
}
