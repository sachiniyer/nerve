# Rebasing this fork onto upstream

Upstream (`ClickHouse/nerve`) moves fast. This fork exists only to add a Signal
channel and a Kubernetes-shaped image; everything else — skills, config, crons —
deliberately lives outside it, in `sachiniyer/nerve-workspace` and
`k3s-configs/nerve/`.

**Keeping that true is what keeps rebasing cheap.** Every line added to an
upstream file is a line that can conflict forever. Adding a new file is free.

---

## The delta, and why it barely conflicts

Don't trust a hardcoded count here — they go stale (this line said "nine
files" long after it was twenty). The live numbers are one command:

```sh
git diff --stat upstream/main...HEAD          # everything
git diff --stat upstream/main...HEAD -- $(git diff --name-only upstream/main...HEAD \
  | while read f; do git cat-file -e upstream/main:$f 2>/dev/null && echo $f; done)   # upstream files only
```

The changes split into two very different groups:

### Group 1 — new files. These can never conflict.

| File | What |
|---|---|
| `nerve/channels/signal.py` | the whole Signal channel |
| `selfcheck.py` | startup assertion |
| `Dockerfile.k8s` | k8s image (upstream gitignores plain `Dockerfile`, which is why ours is named differently — do not rename it back) |
| `web/public/*` | PWA manifest, service worker and icons. Vite copies `public/` into `dist/` verbatim and upstream has no such directory |
| `.github/workflows/k8s-image.yml` | builds and publishes the k8s image — on push to `signal`, daily for new CLI/SDK releases, and on demand. Upstream's `ci.yml` only runs on `main`, so this is also the only thing that tests this branch |
| `web/src/stores/helpers/queueStorage.ts` | persists the web message queue per session (see below) |
| `web/src/stores/messageQueue.test.ts` | the queue's tests |

Git has nothing to merge against. A rebase carries them across untouched.

### Group 2 — patches into upstream files. **The only real risk.**

**Thirteen upstream files** as of 2026-09-26, nearly all additive and
roughly half comment. Every one is listed below with where it anchors. **If
`git diff --name-only` shows an upstream file that is not in this section,
this document is out of date — fix it before rebasing, not after.**

| File | What | Why it must survive a rebase |
|---|---|---|
| `nerve/config.py` | `SignalConfig`, wired into `NerveConfig` | the Signal channel's settings |
| `nerve/gateway/server.py` | Signal start/stop; `/health/activity` | the channel; the deployer's idle check |
| `nerve/agent/backends/claude.py` | refuse `ANTHROPIC_API_KEY` in the CLI env when an OAuth token is set | **billing** — see below |
| `nerve/memory/memu_bridge.py` | memU stays off without a real key | stops a silent 401 loop |
| `web/index.html` | PWA tags + service worker registration | installable app |
| `web/src/components/Chat/ChatInput.tsx` | queue while a turn runs | message queue (#445) |
| `web/src/stores/chatStore.ts` | queue state + actions | message queue (#445) |
| `web/src/stores/handlers/streamingHandlers.ts` | flush the queue on `done` | message queue (#445) |
| `web/src/index.css` | one block appended at the end: phone fixes | iOS zoom-on-focus, tables overflowing the whole conversation, long-word wrapping, clipped `<select>` text |
| `web/src/components/Chat/MessageList.tsx` | stay pinned to the bottom on resize | keyboard opening / composer growing hid the newest message |
| `web/src/components/Chat/{Assistant,Streaming,User}Message.tsx` | avatar `hidden md:flex` | gives phones back 40px of every line |

**`nerve/config.py`** — three insertions:
1. `class SignalConfig` immediately **before** `class TelegramConfig`
2. `signal: SignalConfig = field(...)` in `NerveConfig`, right after the
   `telegram:` field
3. `signal=SignalConfig.from_dict(d.get("signal", {}))` in
   `NerveConfig.from_dict`, right after the `telegram=` line

**`nerve/gateway/server.py`** — two insertions:
1. The "Start Signal channel if enabled" block in `lifespan()`, immediately
   **before** `# Start cron service`
2. `if signal_channel: await signal_channel.stop()` in the shutdown path,
   right after the matching `telegram_channel` line
3. `GET /health/activity` → `{"running": N}`, immediately **after** the
   `/health` route. The out-of-pod deployer polls it so a rollout never kills
   a turn in progress. Unauthenticated like `/health`; exposes only a count.

Every one of them sits next to its Telegram equivalent. **If a rebase
conflicts, the fix is always the same: find what upstream now does for
Telegram, and put the Signal line beside it.** You are never reconstructing
logic, only re-finding an anchor.

**`nerve/agent/backends/claude.py`** — one insertion, in `_build_env`, directly
**before** `if api_key: env["ANTHROPIC_API_KEY"] = api_key`. When
`CLAUDE_CODE_OAUTH_TOKEN` is set, the configured API key is dropped instead of
being injected into the CLI subprocess.

**Dropping this silently moves every agent turn onto pay-per-token billing.**
Upstream injects `config.effective_api_key` as `ANTHROPIC_API_KEY`, and the
CLI prefers that over the OAuth token. It cost $15 in a day before anyone
noticed, because the container's own environment looked clean — the key only
existed in the spawned CLI. `selfcheck.py` fails the rollout if a key is
configured, so a lost patch would fail loudly *if* the config also changed;
it would not catch this patch alone disappearing. Check it by hand:

```sh
grep -n "Refusing to put ANTHROPIC_API_KEY" nerve/agent/backends/claude.py   # expect 1 hit
```

**`nerve/memory/memu_bridge.py`** — one insertion at the top of
`MemUBridge.initialize`: return early when there is no API key, instead of
upstream's fallback to the literal string `"placeholder"` and a 401 on every
call while reporting itself initialized. If upstream adds a real off switch
for memU, drop this and use theirs.

**`web/index.html`** — three insertions, making the UI installable as a PWA:
1. The manifest link, Apple touch icon and web-app meta tags, right after
   upstream's `<link rel="icon" href="/favicon.ico" />`
2. A `theme-color` sync script, immediately **after** upstream's pre-paint
   theme block — it reads the `data-cui-theme` attribute that block sets, so
   it has to come second
3. The service-worker registration, right after
   `<script type="module" src="/src/main.tsx">`

All three are whole blocks appended at the edges of `<head>` and `<body>`,
which is where upstream is least likely to be editing. If upstream adopts its
own PWA setup, **delete ours rather than merging the two** — two manifests and
two service workers is worse than either.

**`web/src/…` — the message queue (upstream issue #445).** The web composer
could not send while a turn ran. Typed messages are now held as removable chips
and sent as ONE message when the turn finishes naturally; after a Stop or an
error they wait for an explicit Send. Three small insertions:

1. `stores/chatStore.ts` — `queued` state, and `enqueueMessage` /
   `removeQueued` / `flushQueue`, placed immediately **before**
   `sendMessage:`. Also widens the declared `sendMessage` type to the three
   arguments its implementation already takes.
2. `stores/handlers/streamingHandlers.ts` — one deferred `flushQueue()` call
   at the end of `handleDone`. Deliberately NOT in `handleStopped` or
   `handleError`.
3. `components/Chat/ChatInput.tsx` — `canSend` no longer requires
   `!isStreaming`; `handleSend` queues mid-turn; Stop and Send are both shown
   while streaming; chips render above the review-loop panel.

**If upstream closes #445, drop ours entirely** rather than merging — two
queues would each think they own the composer. Theirs may well route through
the server instead (the issue suggests `ChannelRouter`), which would make the
client-side queue redundant rather than conflicting.

---

## The CLI and SDK move faster than upstream — on purpose

This deployment tracks the **Claude Code CLI** and the **Claude Agent SDK**
well ahead of upstream nerve's pins. Neither is done by editing
`pyproject.toml` or `uv.lock`:

- The **SDK** is installed *over* the locked version in `Dockerfile.k8s`, after
  `uv sync`. `uv.lock` stays byte-identical to upstream's, so a rebase never
  conflicts on it — which matters, because a lockfile conflict is the one kind
  a rebase cannot resolve by re-finding an anchor.
- The **CLI** is a standalone `npm install -g @anthropic-ai/claude-code`, and
  the copy bundled inside the SDK wheel is deleted so the SDK falls back to it.

Both versions are build args (`CLAUDE_CODE_VERSION`,
`CLAUDE_AGENT_SDK_VERSION`); the defaults in the Dockerfile are the last
known-good pair. `selfcheck.py` fails the rollout if either did not take, or if
the configured default model does not work through the real CLI.

When upstream bumps its own SDK pin, that lands in `uv.lock` via the rebase as
normal and is simply overridden again by the build arg.

## The procedure

```sh
git clone --branch signal git@github.com:sachiniyer/nerve.git /tmp/nervefork
cd /tmp/nervefork
git remote add upstream https://github.com/ClickHouse/nerve.git
git fetch upstream main

# How far behind, and did upstream touch the two files we patch?
git log --oneline HEAD..upstream/main | wc -l
git diff --stat HEAD..upstream/main -- nerve/config.py nerve/gateway/server.py

git rebase upstream/main
```

If it conflicts, it will be in one of those two files. Resolve by the rule
above, then `git add <file> && git rebase --continue`.

```sh
# Confirm the delta is still only what it should be
git diff --stat upstream/main...HEAD

git push --force-with-lease origin signal
```

`--force-with-lease`, not `--force`: a rebase rewrites history, and the lease
refuses if something else pushed in the meantime.

---

## Verify before trusting it

A rebase that produces importable Python has proven nothing. Three checks, in
increasing cost:

```sh
# 1. it parses and the patches survived
python3 -c "import ast;[ast.parse(open(f).read()) for f in \
  ['nerve/config.py','nerve/gateway/server.py','nerve/channels/signal.py']];print('ok')"
grep -c 'SignalConfig' nerve/config.py            # expect 5
grep -c 'signal_channel' nerve/gateway/server.py  # expect 6
grep -c 'Refusing to put ANTHROPIC_API_KEY' nerve/agent/backends/claude.py  # expect 1
grep -c 'memU disabled: no Anthropic API key' nerve/memory/memu_bridge.py   # expect 1
grep -c 'health/activity' nerve/gateway/server.py  # expect 1

# 2. build and roll out — the startup self-check runs here and fails the
#    rollout if config or credentials broke
../k3s-configs/nerve/deploy.sh /tmp/nervefork

# 3. exercise it. A healthy pod proves very little in this deployment.
kubectl -n nerve logs -l app=nerve -c nerve --tail=300 | grep "Registered channel: signal"
```

Then **send it a Signal message from your phone** and check it answers. The
config can load, the channel can register, and inbound can still be broken —
that combination has happened here.

---

## Keeping it cheap

- **New behaviour goes in a new file.** Registration needs a line in
  `server.py`; everything else should not.
- **Never move config into the fork.** Skills, settings and crons belong in the
  workspace repo. The moment config lives here, every rebase is a merge of
  things that have nothing to do with each other.
- **Offer the Signal channel upstream.** Issue-worthy: it is self-contained and
  generally useful. The best outcome is deleting this fork.
- If the *logic* in upstream files ever grows past ~100 lines, that is the
  signal something belongs elsewhere. Comments do not count — they do not
  conflict any harder than the line they explain, and this repo would rather
  have them.
