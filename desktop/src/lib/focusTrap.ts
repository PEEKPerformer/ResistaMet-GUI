// Where Tab goes while a modal dialog is open.

/** Stands for the dialog itself, which holds focus when it has no tab stop. */
export const CONTAINER = -1;

/** The tab stop Tab should move to, CONTAINER for the dialog itself, or null
 *  to let the browser move focus as it would anyway.
 *
 *  `active` is the index of the focused tab stop, or negative when focus is
 *  on the dialog itself or somewhere behind it. */
export function trappedTabStop(count: number, active: number, backwards: boolean): number | null {
  if (count === 0) return CONTAINER;
  if (active < 0 || active >= count) return backwards ? count - 1 : 0;
  if (backwards && active === 0) return count - 1;
  if (!backwards && active === count - 1) return 0;
  return null;
}
