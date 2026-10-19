"use client";
/**
 * The invitations page's client composition (V1.1 §4.2): the create
 * form and the lifecycle list are independent panels EXCEPT for one
 * nudge — a fresh issuance bumps the list's refreshKey so the new row
 * appears without a manual 刷新 (the e2e lifecycle chain's own pin).
 */
import { useState } from "react";

import { InvitationListPanel } from "./InvitationListPanel";
import { StaffInvitations } from "./StaffInvitations";

export function InvitationsWorkspace() {
  const [refreshKey, setRefreshKey] = useState(0);
  return (
    <>
      <StaffInvitations onIssued={() => setRefreshKey((key) => key + 1)} />
      <InvitationListPanel refreshKey={refreshKey} />
    </>
  );
}
