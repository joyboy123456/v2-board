/** REST replay and live SSE may overlap; seq is the identity inside one stream. */
export function mergeBySeq<T extends { seq: number }>(previous: T[], incoming: T[]): T[] {
  const messages = new Map(previous.map(message => [message.seq, message]));
  for (const message of incoming) messages.set(message.seq, message);
  return [...messages.values()].sort((a, b) => a.seq - b.seq);
}

/** Only acknowledge a contiguous prefix: live traffic can arrive before history. */
export function contiguousSeq(messages: { seq: number }[]): number {
  let after = 0;
  for (const message of messages) {
    if (message.seq !== after + 1) break;
    after = message.seq;
  }
  return after;
}
