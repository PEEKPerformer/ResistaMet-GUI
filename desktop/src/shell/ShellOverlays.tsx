// What the desktop shell puts on top of the app. Mounted beside the app, not
// inside it, so none of this depends on the app having found its backend or
// on its views rendering without error.

import { BackendExitAlert } from "./BackendExitAlert";
import { CloseGuard } from "./CloseGuard";
import { SimulatedBadge } from "./SimulatedBadge";
import { inTauri } from "./tauri";

export function ShellOverlays() {
  if (!inTauri()) return null;
  return (
    <>
      <SimulatedBadge />
      <CloseGuard />
      <BackendExitAlert />
    </>
  );
}
