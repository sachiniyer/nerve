// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Same storage shim as chatStore.test.ts: the store reads localStorage at
// module init, so a real in-memory Storage must exist before the import.
function installStorage(): void {
  const data = new Map<string, string>();
  const storage = {
    getItem: (k: string) => (data.has(k) ? data.get(k)! : null),
    setItem: (k: string, v: string) => void data.set(k, String(v)),
    removeItem: (k: string) => void data.delete(k),
    clear: () => data.clear(),
    key: (i: number) => [...data.keys()][i] ?? null,
    get length() { return data.size; },
  };
  for (const target of [globalThis, globalThis.window]) {
    if (target) Object.defineProperty(target, 'localStorage', { value: storage, configurable: true, writable: true });
  }
}
installStorage();

vi.mock('../api/client', () => ({
  api: {
    listSessions: vi.fn().mockResolvedValue([]),
    listArchivedSessions: vi.fn().mockResolvedValue([]),
    listSystemSessions: vi.fn().mockResolvedValue([]),
  },
}));
vi.mock('../api/websocket', () => ({
  ws: { switchSession: vi.fn(), send: vi.fn(), connect: vi.fn(), sendMessage: vi.fn(() => 'sent') },
}));

const { ws } = await import('../api/websocket');
const { useChatStore } = await import('./chatStore');
const { handleDone, handleStopped, handleError } = await import('./handlers/streamingHandlers');

const SESSION = 'sess-1';
const get = () => useChatStore.getState();
const set = useChatStore.setState;
const flushTimers = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  set({
    activeSession: SESSION,
    virtualSession: null,
    messages: [],
    streamingBlocks: [],
    isStreaming: false,
    queued: {},
    // handleDone refreshes the sidebar; irrelevant here and noisy against mocks.
    loadSessions: vi.fn().mockResolvedValue(undefined),
  });
});

describe('message queue (web composer, upstream #445)', () => {
  it('holds messages per session without sending them', () => {
    set({ isStreaming: true });
    get().enqueueMessage('first');
    get().enqueueMessage('second');

    expect(get().queued[SESSION].map((q) => q.content)).toEqual(['first', 'second']);
    expect(ws.sendMessage).not.toHaveBeenCalled();
  });

  it('does not wipe the reply that is still streaming', () => {
    // The trap this design avoids: sendMessage resets streamingBlocks, so
    // sending mid-turn would erase the partial answer on screen.
    const partial = [{ type: 'text' as const, content: 'half an answer' }];
    set({ isStreaming: true, streamingBlocks: partial });
    get().enqueueMessage('follow-up');

    expect(get().streamingBlocks).toBe(partial);
    expect(get().messages).toHaveLength(0);
  });

  it('removes a queued message', () => {
    set({ isStreaming: true });
    get().enqueueMessage('keep');
    get().enqueueMessage('drop');
    const drop = get().queued[SESSION].find((q) => q.content === 'drop')!;
    get().removeQueued(drop.id);

    expect(get().queued[SESSION].map((q) => q.content)).toEqual(['keep']);
  });

  it('sends the whole queue as ONE message when the turn finishes', async () => {
    set({ isStreaming: true });
    get().enqueueMessage('actually use the shared calendar');
    get().enqueueMessage('and add the flight number');

    handleDone({ type: 'done' } as never, get, set);
    await flushTimers();

    expect(ws.sendMessage).toHaveBeenCalledTimes(1);
    expect(vi.mocked(ws.sendMessage).mock.calls[0][0]).toBe(
      'actually use the shared calendar\n\nand add the flight number',
    );
    expect(vi.mocked(ws.sendMessage).mock.calls[0][1]).toBe(SESSION);
    expect(get().queued[SESSION]).toEqual([]);
  });

  it('merges attachments from every queued message', async () => {
    set({ isStreaming: true });
    get().enqueueMessage('see this', ['f1']);
    get().enqueueMessage('and this', ['f2']);

    handleDone({ type: 'done' } as never, get, set);
    await flushTimers();

    expect(vi.mocked(ws.sendMessage).mock.calls[0][2]).toEqual(['f1', 'f2']);
  });

  it('does NOT auto-send after a stop — the queue waits for the user', async () => {
    set({ isStreaming: true });
    get().enqueueMessage('only if it finished');

    handleStopped({ type: 'stopped' } as never, get, set);
    await flushTimers();

    expect(ws.sendMessage).not.toHaveBeenCalled();
    expect(get().queued[SESSION]).toHaveLength(1);
  });

  it('does NOT auto-send after an error', async () => {
    set({ isStreaming: true });
    get().enqueueMessage('retry later');

    handleError({ type: 'error', error: 'boom' } as never, get, set);
    await flushTimers();

    expect(ws.sendMessage).not.toHaveBeenCalled();
    expect(get().queued[SESSION]).toHaveLength(1);
  });

  it('flushQueue is a no-op while a turn is still running', () => {
    set({ isStreaming: true });
    get().enqueueMessage('wait');
    get().flushQueue();

    expect(ws.sendMessage).not.toHaveBeenCalled();
    expect(get().queued[SESSION]).toHaveLength(1);
  });

  it('never delivers a queued item twice', async () => {
    set({ isStreaming: true });
    get().enqueueMessage('once');

    handleDone({ type: 'done' } as never, get, set);
    await flushTimers();
    // A second done (e.g. the flushed turn finishing) must find nothing left.
    set({ isStreaming: false });
    handleDone({ type: 'done' } as never, get, set);
    await flushTimers();

    expect(ws.sendMessage).toHaveBeenCalledTimes(1);
  });

  it('keeps queues separate per session', () => {
    set({ isStreaming: true });
    get().enqueueMessage('for session 1');
    set({ activeSession: 'sess-2' });
    get().enqueueMessage('for session 2');

    expect(get().queued[SESSION].map((q) => q.content)).toEqual(['for session 1']);
    expect(get().queued['sess-2'].map((q) => q.content)).toEqual(['for session 2']);
  });

  it('persists the queue so a killed PWA does not lose it', () => {
    set({ isStreaming: true });
    get().enqueueMessage('survive a reload');

    const raw = localStorage.getItem(`nerve_queue_${SESSION}`);
    expect(JSON.parse(raw!).map((q: { content: string }) => q.content)).toEqual(['survive a reload']);
  });

  it('removes the persisted key once the queue is sent', async () => {
    set({ isStreaming: true });
    get().enqueueMessage('gone after send');
    handleDone({ type: 'done' } as never, get, set);
    await flushTimers();

    expect(localStorage.getItem(`nerve_queue_${SESSION}`)).toBeNull();
  });
});
