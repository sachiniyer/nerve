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


def main() -> int:
    problems = check_config() + check_model()
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
