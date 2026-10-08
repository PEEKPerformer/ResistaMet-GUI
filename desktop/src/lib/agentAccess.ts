// What the "Allow AI agents" switch cannot say by itself. The switch is the
// stored setting; GET /agents is what is in force, and the two differ when
// the backend was started with --allow-agents or another backend holds the
// agents' connection file (docs/api.md, Agent access).

/** The line under the switch, or null when the switch tells the truth or
 *  what is in force is unknown. `stored` is the saved allow_agents. */
export function agentAccessNote(stored: unknown, enabled: boolean | null): string | null {
  if (enabled === null) return null;
  const storedOn = stored === true;
  if (enabled && !storedOn) return "On for this session (started with --allow-agents)";
  if (!enabled && storedOn) return "Off: another ResistaMet backend is serving agents";
  return null;
}
