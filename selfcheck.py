#!/usr/bin/env python3
"""Startup self-check — fail the rollout instead of running broken.

This exists because of a specific, repeated failure mode. Over one day of
bringing this deployment up, four separate bugs produced a pod that was
``Running``, passing its health checks, and completely broken:

* an invalid ``CLAUDE_CODE_OAUTH_TOKEN`` — logged one soft INFO line and the
  gateway came up fine; it failed only when a human typed something;
* a config directory misdetected to the working directory — every setting we
  had written was ignored in favour of upstream defaults, including a
  permission control, and nothing said so;
* a workspace ``git merge`` that failed while reporting success, so the pod
  started on stale config;
* Proton Bridge self-updating into a binary the image cannot execute.

Each was found by a person noticing something odd, hours or days later. That
is the expensive way to find them.

So: check the things that are silently wrong when they are wrong, and exit
non-zero if any of them is. A non-zero exit here means the container never
starts, the rollout never completes, and ``kubectl rollout status`` fails —
which is a loud, immediate, correctly-timed failure instead of a quiet one.

Deliberately NOT a liveness probe. This runs once at start: the model call
costs real money and probing it every ten seconds would be absurd. What this
catches is a bad *rollout*, which is when these faults are actually introduced.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Values we set deliberately and would want to know about silently losing.
# Each is a setting whose wrongness is invisible at runtime.
EXPECTED = {
    "timezone": "America/Los_Angeles",
    # A permission control. If this is True, background sub-agents inherit
    # Write/Edit/Bash — which is exactly what the config-directory bug did.
    "agent.background_agent_permissions": False,
}

MODEL = "claude-haiku-4-5-20251001"

# Binaries the skills depend on. A skill referencing a tool that is not in the
# image fails at the moment the agent tries to use it — in conversation, in
# front of the person — with a bare "not found" and no hint that a build
# dropped it.
#
# This list exists because exactly that happened: link-cli was added to a
# working copy, never committed, and a later rebuild from a clean clone
# silently removed it. Nothing noticed until someone looked.
REQUIRED_BINARIES = ["gog", "himalaya", "link-cli", "plann", "git", "gh"]


def _get(obj, dotted: str):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def check_config() -> list[str]:
    """Read config back out of the loader, not off disk.

    Reading the file would prove only that we wrote it. The failure being
    guarded against is the process not reading the file we wrote.
    """
    sys.path.insert(0, "/nerve")
    from nerve.config import get_config

    cfg = get_config()
    problems = []
    for key, want in EXPECTED.items():
        try:
            got = _get(cfg, key)
        except AttributeError:
            problems.append(f"config: {key} is missing entirely")
            continue
        if got != want:
            problems.append(f"config: {key} is {got!r}, expected {want!r}")
    return problems


def check_model() -> list[str]:
    """Make one real, minimal model call.

    A credential that is present, correctly shaped and expired looks identical
    to a working one until something uses it. The only honest check is use.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not token and not key:
        return ["auth: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set"]

    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if key:
        headers["x-api-key"] = key
    else:
        headers["authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 4,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return [f"auth: model call returned HTTP {resp.status}"]
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        return [f"auth: model call failed HTTP {e.code} {detail}"]
    except Exception as e:
        # A network blip at boot should not brick the rollout. Say so and let
        # it through: this check exists for credentials, not connectivity.
        print(f"selfcheck: WARN could not reach the API ({e}); skipping auth check",
              file=sys.stderr)
    return []


def check_binaries() -> list[str]:
    """Every CLI a skill depends on must be on PATH."""
    import shutil

    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    return [f"tooling: {b} is not installed (a skill depends on it)" for b in missing]


# Directories that MUST be on their own volume rather than the container's
# writable layer. Anything here is state a restart would otherwise destroy
# without a word.
#
# /root/.claude earned its place: it holds the Agent SDK's conversation
# .jsonl transcripts, which are what a resume actually reads. nerve keeps the
# session mapping in its own database on a different volume, so when this one
# was missing the mapping survived a restart and the transcript it pointed at
# did not. Every conversation silently reset on every deploy, and the only
# visible symptom was the agent saying it was "starting fresh without
# context" while still knowing the topic.
PERSISTENT_DIRS = ["/root/.claude", "/root/.nerve", "/root/.config"]


def check_persistence() -> list[str]:
    """Each persistent directory must be a mount point, not the rootfs.

    A directory on the container's writable layer and one backed by a PVC are
    indistinguishable by listing them — the difference only shows up a restart
    later, as missing data. Comparing st_dev against the parent catches it now.
    """
    problems = []
    for d in PERSISTENT_DIRS:
        try:
            if not os.path.isdir(d):
                problems.append(f"persistence: {d} does not exist")
            elif os.stat(d).st_dev == os.stat(os.path.dirname(d) or "/").st_dev:
                problems.append(
                    f"persistence: {d} is on the container filesystem, not a "
                    f"volume — its contents will be lost on every restart"
                )
        except OSError as e:
            problems.append(f"persistence: cannot stat {d}: {e}")
    return problems


def check_claude_config_dir() -> list[str]:
    """CLAUDE_CONFIG_DIR must agree with where nerve looks for transcripts.

    nerve's validate_resume_target() hardcodes ~/.claude/projects. If the CLI
    is pointed somewhere else the two disagree about where history lives, and
    the failure is silent context loss rather than an error.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured and os.path.realpath(configured) != os.path.realpath(
        os.path.expanduser("~/.claude")
    ):
        return [
            f"persistence: CLAUDE_CONFIG_DIR={configured} but nerve reads "
            f"transcripts from ~/.claude/projects — resumes will silently "
            f"start fresh"
        ]
    return []


def check_subscription_only() -> list[str]:
    """Inference must run on the subscription, never on an API key.

    This is a money check, not a style one. nerve's _build_env injects
    config.effective_api_key into the spawned Claude Code CLI as
    ANTHROPIC_API_KEY, and the CLI prefers that over CLAUDE_CODE_OAUTH_TOKEN.
    So an api key set anywhere config can see it — including
    `anthropic_api_key` in the workspace settings.yaml, which was set for
    memU — silently moves EVERY agent turn onto pay-per-token billing.

    It did exactly that here: $15 of credits in a day, no error, no log line,
    and the container's own environment looked clean because the key only
    appears in the child process. The fork now refuses the injection, and this
    fails the rollout if the configuration that caused it ever comes back.
    """
    problems = []
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        problems.append(
            "billing: CLAUDE_CODE_OAUTH_TOKEN is not set — the subscription "
            "is the only permitted inference path on this deployment"
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        problems.append(
            "billing: ANTHROPIC_API_KEY is set in the environment; the CLI "
            "prefers it over the subscription token and every turn would be "
            "billed per-token"
        )
    try:
        from nerve.config import get_config

        if (get_config().effective_api_key or "").strip():
            problems.append(
                "billing: an anthropic_api_key is configured; nerve injects it "
                "into the agent subprocess, which moves the whole agent off "
                "the subscription onto per-token billing"
            )
    except Exception as e:
        problems.append(f"billing: could not read the api key config: {e}")
    return problems


def main() -> int:
    problems = (
        check_config()
        + check_subscription_only()
        + check_binaries()
        + check_persistence()
        + check_claude_config_dir()
        + check_model()
    )
    if problems:
        print("=" * 68, file=sys.stderr)
        print("STARTUP SELF-CHECK FAILED — refusing to start", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        print("", file=sys.stderr)
        print("The pod is being failed deliberately. Starting anyway would give", file=sys.stderr)
        print("a healthy-looking deployment that is quietly wrong, which is how", file=sys.stderr)
        print("every one of these faults went unnoticed the first time.", file=sys.stderr)
        print("=" * 68, file=sys.stderr)
        return 1
    print("selfcheck: config and credentials OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
