// Replies to concurrent requests can arrive out of order: the status poll
// sent just before a Stop may be answered after the Stop's own reply, and
// would put "running" back on screen. Each request takes a ticket when it is
// sent; a reply is used only if no later-sent request has been answered.

export class ReplyOrder {
  private lastSent = 0;
  private lastAccepted = 0;

  /** Call when the request goes out; keep the ticket for its reply. */
  sent(): number {
    this.lastSent += 1;
    return this.lastSent;
  }

  /** Whether the reply holding this ticket is still the newest word. */
  accepts(ticket: number): boolean {
    if (ticket <= this.lastAccepted) return false;
    this.lastAccepted = ticket;
    return true;
  }
}
