// Per-session queued-message persistence.
//
// Messages typed while the agent is mid-turn are held client-side and sent
// when the turn ends (see chatStore.enqueueMessage / flushQueue). They are
// persisted for the same reason drafts are: an installed PWA on a phone gets
// backgrounded and killed constantly, and a queued follow-up that silently
// vanished would be worse than never having queued it.
//
// Same shape as draftStorage — one key per session, every access quota-safe,
// and a failure to persist only means the queue lives in memory.

export interface QueuedMessage {
  id: string;
  content: string;
  fileIds?: string[];
  imageBlocks?: Array<{ url: string; filename: string; media_type: string }>;
}

const PREFIX = 'nerve_queue_';

const keyFor = (sessionId: string) => `${PREFIX}${sessionId}`;

/** Read every persisted queue into a { sessionId: messages } map (store hydration). */
export function loadQueues(): Record<string, QueuedMessage[]> {
  const out: Record<string, QueuedMessage[]> = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(PREFIX)) continue;
      try {
        const parsed = JSON.parse(localStorage.getItem(k) || '[]');
        if (Array.isArray(parsed) && parsed.length > 0) {
          out[k.slice(PREFIX.length)] = parsed;
        }
      } catch { /* one corrupt key must not take the others down */ }
    }
  } catch { /* storage unavailable */ }
  return out;
}

/** Persist one session's queue; an empty queue removes the key. */
export function persistQueue(sessionId: string, queue: QueuedMessage[]): void {
  try {
    if (queue.length === 0) localStorage.removeItem(keyFor(sessionId));
    else localStorage.setItem(keyFor(sessionId), JSON.stringify(queue));
  } catch { /* quota or disabled storage — the queue stays in memory */ }
}
