// Why Start is dead when the backend has stopped answering, said in the view
// the operator is looking at rather than only by a chip in the header, with a
// way to try again without waiting for the next poll.

import { useState } from "react";
import { useServices } from "../app/AppContext";
import { useSession } from "../state/session";
import { Button, Notice } from "./ui";

export function BackendNotice() {
  const { backendReachable } = useSession();
  const { retryConnection } = useServices();
  const [busy, setBusy] = useState(false);

  if (backendReachable !== false) return null;

  const retry = async () => {
    setBusy(true);
    try {
      await retryConnection();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Notice
      tone="danger"
      action={
        <Button size="sm" disabled={busy} onClick={() => void retry()}>
          {busy ? "Retrying…" : "Retry"}
        </Button>
      }
    >
      The measurement backend is not answering. Runs cannot start until it does.
    </Notice>
  );
}
