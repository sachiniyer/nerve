# Rebasing this fork onto upstream

Upstream (`ClickHouse/nerve`) moves fast. This fork exists only to add a Signal
channel and a Kubernetes-shaped image; everything else — skills, config, crons —
deliberately lives outside it, in `sachiniyer/nerve-workspace` and
`k3s-configs/nerve/`.

**Keeping that true is what keeps rebasing cheap.** Every line added to an
upstream file is a line that can conflict forever. Adding a new file is free.

---

## The delta, and why it barely conflicts

Six commits, ~850 insertions, nine files. They split into two very different
groups:

### Group 1 — new files. These can never conflict.

| File | What |
|---|---|
| `nerve/channels/signal.py` | the whole Signal channel |
| `selfcheck.py` | startup assertion |
| `Dockerfile.k8s` | k8s image (upstream gitignores plain `Dockerfile`, which is why ours is named differently — do not rename it back) |
| `web/public/*` | PWA manifest, service worker and icons. Vite copies `public/` into `dist/` verbatim and upstream has no such directory |
| `web/src/stores/helpers/queueStorage.ts` | persists the web message queue per session (see below) |
| `web/src/stores/messageQueue.test.ts` | the queue's tests |

Git has nothing to merge against. A rebase carries them across untouched.

### Group 2 — patches into upstream files. **The only real risk.**

Just **three files, ~124 lines**, all additive — and about half of that is
comment, so the count overstates it.

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

Every one of them sits next to its Telegram equivalent. **If a rebase
conflicts, the fix is always the same: find what upstream now does for
Telegram, and put the Signal line beside it.** You are never reconstructing
logic, only re-finding an anchor.

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
grep -c 'SignalConfig' nerve/config.py            # expect 4
grep -c 'signal_channel' nerve/gateway/server.py  # expect 6

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
