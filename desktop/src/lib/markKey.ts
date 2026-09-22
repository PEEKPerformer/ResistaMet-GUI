// Whether a keydown is the operator pressing M to mark the moment.
//
// Not while typing in a field, not as part of a shortcut (Cmd+M minimises
// the window on macOS), and once per press: a held key repeats, and each
// repeat would write another mark into the data.

export interface KeyPress {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  repeat: boolean;
  /** tagName of the event target, if it is an element. */
  targetTag: string | null;
}

export function isMarkKey(press: KeyPress): boolean {
  if (press.key !== "m" && press.key !== "M") return false;
  if (press.metaKey || press.ctrlKey || press.altKey || press.repeat) return false;
  return !(press.targetTag !== null && /^(INPUT|TEXTAREA|SELECT)$/.test(press.targetTag));
}
