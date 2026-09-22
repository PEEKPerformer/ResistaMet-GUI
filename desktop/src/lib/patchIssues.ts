// What the Settings dialog makes of a refused Save.

export interface PatchIssue {
  section: string;
  key: string;
  message: string;
}

/** The per-key issues in a refused PATCH. The API client hands a structured
 *  error detail over as JSON text; anything else has no issues to show. */
export function patchIssues(detail: string): PatchIssue[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(detail);
  } catch {
    return [];
  }
  const issues = (parsed as { issues?: unknown } | null)?.issues;
  if (!Array.isArray(issues)) return [];
  return issues.filter(
    (issue): issue is PatchIssue =>
      typeof issue === "object" &&
      issue !== null &&
      typeof (issue as PatchIssue).section === "string" &&
      typeof (issue as PatchIssue).key === "string" &&
      typeof (issue as PatchIssue).message === "string",
  );
}
