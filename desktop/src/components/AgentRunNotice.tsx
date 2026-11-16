// A run an AI agent started, not the person at the window. Shown on the view
// of that run's mode for as long as it is the run on screen, so whoever looks
// at the plot knows who chose these settings (docs/design/mcp_layer.md M6).

import type { Mode } from "../generated/settings";
import { useSession } from "../state/session";
import { Notice } from "./ui";

export function AgentRunNotice({ mode }: { mode: Mode }) {
  const { runStartedBy, status } = useSession();

  if (runStartedBy !== "agent" || status?.mode !== mode) return null;

  return <Notice tone="info">Started by an AI agent</Notice>;
}
