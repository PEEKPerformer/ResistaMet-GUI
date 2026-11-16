// A decision the run is blocked on, presented so it cannot be missed.
//
// The backend raises a prompt and waits; the run does not continue until it
// is answered or stopped. So the dialog cannot be dismissed — the options are
// the only way out — and it renders whichever prompt the session reports,
// so a reconnecting UI shows the question the run is still waiting on.

import { useState } from "react";
import { useApi } from "../../app/AppContext";
import type { PendingPrompt } from "../../lib/api";
import { ApiError } from "../../lib/api";
import { Button, Dialog, Notice } from "../ui";
import styles from "./dialogs.module.css";

interface Props {
  prompt: PendingPrompt;
}

export function PromptDialog({ prompt }: Props) {
  switch (prompt.kind) {
    case "safety_voltage_ack":
      return <SafetyPrompt prompt={prompt} />;
    default:
      // vdp_geometry is rendered inline by the vdP view; anything else the
      // backend may add later gets the generic form.
      return prompt.kind === "vdp_geometry" ? null : <GenericPrompt prompt={prompt} />;
  }
}

function useAnswer(prompt: PendingPrompt) {
  const api = useApi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const answer = async (choice: string, fields: Record<string, unknown> = {}) => {
    setBusy(true);
    setError(null);
    try {
      await api.answerPrompt(prompt.prompt_id, choice, fields);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };
  return { answer, busy, error };
}

function SafetyPrompt({ prompt }: Props) {
  const { answer, busy, error } = useAnswer(prompt);
  const [silence, setSilence] = useState(false);
  const detail = prompt.detail as { voltage_v?: number; threshold_v?: number; reason?: string; message?: string };

  return (
    <Dialog
      title="Touch-safety voltage"
      dismissable={false}
      footer={
        <>
          <Button variant="ghost" disabled={busy} onClick={() => void answer("cancel")}>
            Cancel run
          </Button>
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => void answer("acknowledge", silence ? { silence_for_profile: true } : {})}
          >
            Acknowledge and energize
          </Button>
        </>
      }
    >
      <div className={styles.safety}>
        <div className={`${styles.safetyVoltage} num`}>{detail.voltage_v ?? "—"} V</div>
        <p>
          {detail.reason ?? "The configured voltage"} is at or above the {detail.threshold_v ?? 30} V touch-safety threshold
          (IEC 61010-1 SELV). Output is off.
        </p>
        <label className={styles.checkbox}>
          <input type="checkbox" checked={silence} onChange={(e) => setSilence(e.target.checked)} />
          Don&apos;t ask again for this profile
        </label>
        {error ? <Notice tone="danger">{error}</Notice> : null}
      </div>
    </Dialog>
  );
}

function GenericPrompt({ prompt }: Props) {
  const { answer, busy, error } = useAnswer(prompt);
  return (
    <Dialog
      title={prompt.kind.replace(/_/g, " ")}
      dismissable={false}
      footer={prompt.options.map((option) => (
        <Button key={option} disabled={busy} onClick={() => void answer(option)}>
          {option}
        </Button>
      ))}
    >
      <pre className={styles.detail}>{JSON.stringify(prompt.detail, null, 2)}</pre>
      {error ? <Notice tone="danger">{error}</Notice> : null}
    </Dialog>
  );
}
