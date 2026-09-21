// The last run could not confirm the instrument output is off: its link to
// the instrument died before the :OUTP OFF landed, or the read-back said on.
// The source may still be driving the sample, so this is said where the
// operator is looking, not only as a line in the log, and stays until they
// dismiss it or the backend reports the output off (the next connection
// turns it off first thing).

import { dismissOutputNotice, useSession } from "../state/session";
import { Button, Notice } from "./ui";

export function OutputNotice() {
  const { outputUnverified } = useSession();

  if (!outputUnverified) return null;

  return (
    <Notice
      tone="danger"
      action={
        <Button size="sm" onClick={dismissOutputNotice}>
          Dismiss
        </Button>
      }
    >
      Instrument output may still be ON — check the front panel. The next connection turns it off.
    </Notice>
  );
}
