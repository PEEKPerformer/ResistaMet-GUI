// Closing the window ends the app, and the app ending stops the run. The
// shell holds the window open and asks here; this asks the backend whether a
// run is active and, if one is, asks the operator — as the PySide6 app does,
// with staying open as the default.

import { useEffect, useRef, useState } from "react";
import { ApiClient } from "../lib/api";
import type { BackendInfo } from "../lib/backend";
import type { SessionStatus } from "../generated/session";
import { decideClose, type CloseDecision } from "./closeDecision";
import { ShellAlert, ShellButton } from "./ShellAlert";
import { CLOSE_REQUESTED, invokeShell, onShellEvent } from "./tauri";

/** How long the backend gets to say what the session is doing. */
const STATUS_TIMEOUT_MS = 3000;

async function sessionStatus(): Promise<SessionStatus | null> {
  try {
    const info = await invokeShell<BackendInfo>("backend_info");
    const timeout = new Promise<null>((resolve) => setTimeout(() => resolve(null), STATUS_TIMEOUT_MS));
    return await Promise.race([new ApiClient(info).status(), timeout]);
  } catch {
    return null;
  }
}

type Question = Exclude<CloseDecision, { kind: "close" }>;

export function CloseGuard() {
  const [question, setQuestion] = useState<Question | null>(null);
  const [closing, setClosing] = useState(false);
  // One question at a time: a second close request while this one is being
  // worked out or is on screen changes nothing.
  const busy = useRef(false);

  useEffect(
    () =>
      onShellEvent<null>(CLOSE_REQUESTED, () => {
        void invokeShell("close_request_seen");
        if (busy.current) return;
        busy.current = true;
        void sessionStatus().then((status) => {
          const decision = decideClose(status);
          if (decision.kind === "close") void invokeShell("confirm_close");
          else setQuestion(decision);
        });
      }),
    [],
  );

  if (question === null) return null;

  const keep = () => {
    busy.current = false;
    setQuestion(null);
    void invokeShell("cancel_close");
  };
  const close = () => {
    setClosing(true);
    void invokeShell("confirm_close");
  };

  return (
    <ShellAlert
      title="Exit confirmation"
      actions={
        <>
          <ShellButton variant="danger" disabled={closing} onClick={close}>
            {closing ? "Stopping…" : "Stop and exit"}
          </ShellButton>
          <ShellButton variant="primary" isDefault disabled={closing} onClick={keep}>
            Keep running
          </ShellButton>
        </>
      }
    >
      {question.kind === "ask" ? (
        <>
          <p>A measurement{question.mode ? ` (${question.mode})` : ""} is currently running.</p>
          <p>Stopping may result in incomplete data.</p>
        </>
      ) : (
        <>
          <p>The measurement backend is not answering, so a running measurement cannot be ruled out.</p>
          <p>Exiting stops any run in progress.</p>
        </>
      )}
    </ShellAlert>
  );
}
