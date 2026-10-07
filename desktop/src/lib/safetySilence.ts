// The touch-safety silence: what the prompt's answer asks for, and whether
// a timed one is in force for Settings to show.
//
// Whether a run is asked is the backend's decision (safety.warning_silenced);
// nothing here feeds it. The answer's fields are the SafetyAckFields
// contract, and the backend saves them to the run's profile.

import type { SafetyAckFields } from "../generated/session";

export type SilenceChoice = "ask" | "week" | "forever";

export const SILENCE_CHOICES: readonly { value: SilenceChoice; label: string }[] = [
  { value: "ask", label: "Ask every time" },
  { value: "week", label: "Don't ask again for 7 days" },
  { value: "forever", label: "Don't ask again" },
];

/** The fields an acknowledge carries for a choice. A cancel carries none. */
export function silenceFields(choice: SilenceChoice): SafetyAckFields {
  switch (choice) {
    case "week":
      return { silence_for_days: 7 };
    case "forever":
      return { silence_for_profile: true };
    default:
      return {};
  }
}

/** When a timed silence on this measurement section runs out, if it has not
 *  yet at `nowMs`; null when there is none or it is over. */
export function timedSilenceEnd(measurement: Record<string, unknown>, nowMs: number): Date | null {
  const until = measurement.safety_voltage_warn_silenced_until;
  if (typeof until !== "number" || !Number.isFinite(until)) return null;
  return until * 1000 > nowMs ? new Date(until * 1000) : null;
}

/** The measurement keys that put the warning back on: no timed silence and
 *  no sticky one. */
export const UNSILENCED = {
  safety_voltage_warn_silenced_until: null,
  safety_voltage_warn_silenced: false,
} as const;
