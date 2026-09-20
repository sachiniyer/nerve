# Rebasing this fork onto upstream

Upstream (`ClickHouse/nerve`) moves fast. This fork exists only to add a Signal
channel and a Kubernetes-shaped image; everything else — skills, config, crons —
deliberately lives outside it, in `sachiniyer/nerve-workspace` and
`k3s-configs/nerve/`.

**Keeping that true is what keeps rebasing cheap.** Every line added to an
upstream file is a line that can conflict forever. Adding a new file is free.

---

## The delta, and why it barely conflicts

Five commits, ~750 insertions, five files. They split into two very different
groups:

### Group 1 — new files. These can never conflict.

| File | What |
|---|---|
| `nerve/channels/signal.py` | the whole Signal channel |
| `selfcheck.py` | startup assertion |
| `Dockerfile.k8s` | k8s image (upstream gitignores plain `Dockerfile`, which is why ours is named differently — do not rename it back) |

Git has nothing to merge against. A rebase carries them across untouched.

### Group 2 — patches into upstream files. **The only real risk.**

Just **two files, 58 lines total**, all additive:

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
- If the delta ever grows past ~100 lines in upstream files, that is the signal
  something belongs elsewhere.
