"""Tests for this fork's patches into upstream files, and the Signal channel.

A new file on purpose (see REBASE.md): it can never conflict on a rebase. The
patches it covers are the ones that fail silently when lost — the billing
guard most of all, whose loss looks like a healthy deployment that happens to
be spending money.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from nerve.config import NerveConfig, SignalConfig


# --------------------------------------------------------------------------- #
#  Billing: no API key reaches the CLI while a subscription token exists        #
# --------------------------------------------------------------------------- #


def _backend(*, api_key: str = "", oauth: str = ""):
    from nerve.agent.backends.claude import ClaudeBackend

    config = SimpleNamespace(
        provider=SimpleNamespace(
            is_bedrock=False, aws_region="", aws_profile="",
            aws_access_key_id="", aws_secret_access_key="",
        ),
        proxy=SimpleNamespace(enabled=False, host="", port=0),
        anthropic_base_url="",
        effective_api_key=api_key,
        claude_oauth_token=oauth,
        agent=SimpleNamespace(model_aliases={}, agent_teams=False),
    )
    return ClaudeBackend(SimpleNamespace(config=lambda: config))


class TestBillingGuard:
    def test_env_token_drops_configured_key(self, monkeypatch):
        # The deployment's shape: the token arrives in the environment from
        # the k8s Secret, and a key turns up in config.
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-REPLACE")
        env = _backend(api_key="sk-ant-REPLACE0000000000")._build_env()
        assert "ANTHROPIC_API_KEY" not in env

    def test_config_token_drops_configured_key(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        env = _backend(api_key="sk-ant-REPLACE0000000000", oauth="sk-ant-oat01-REPLACE")._build_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-REPLACE"

    def test_key_alone_is_still_passed(self, monkeypatch):
        # The guard refuses a key only when there is a subscription to fall
        # back on; without one the key is the only way to run at all.
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        env = _backend(api_key="sk-ant-REPLACE0000000000")._build_env()
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-REPLACE0000000000"


# --------------------------------------------------------------------------- #
#  memU stays off without a real key                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_memu_does_not_start_without_a_key(monkeypatch):
    from nerve.memory.memu_bridge import MemUBridge

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-REPLACE")
    bridge = MemUBridge(NerveConfig())
    started = AsyncMock()
    monkeypatch.setattr(bridge, "_start_memu_loop", started)

    assert await bridge.initialize() is False
    assert bridge._available is False
    started.assert_not_called()


# --------------------------------------------------------------------------- #
#  SignalConfig                                                                 #
# --------------------------------------------------------------------------- #


class TestSignalConfig:
    def test_off_by_default(self):
        assert NerveConfig().signal.enabled is False

    def test_string_false_stays_off(self):
        # bool("false") is True; the reason this uses upstream's coercion.
        assert SignalConfig.from_dict({"enabled": "false", "number": "+1"}).enabled is False

    def test_bare_string_is_one_number(self):
        cfg = SignalConfig.from_dict({"allowed_numbers": "+15550001111"})
        assert cfg.allowed_numbers == ["+15550001111"]

    def test_wired_into_nerve_config(self):
        cfg = NerveConfig.from_dict({"signal": {"enabled": True, "number": "+15550001111"}})
        assert cfg.signal.enabled is True
        assert cfg.signal.number == "+15550001111"


# --------------------------------------------------------------------------- #
#  Signal channel                                                               #
# --------------------------------------------------------------------------- #

OWN = "+15550000000"
FRIEND = "+15550002222"


def _signal(outbound: list[str] | None = None, router=None):
    from nerve.channels.signal import SignalChannel

    cfg = SignalConfig(
        enabled=True, number=OWN, allowed_numbers=[OWN],
        outbound_allowed_numbers=outbound or [],
    )
    return SignalChannel(lambda: SimpleNamespace(signal=cfg), router or SimpleNamespace())


class TestSignalOutbound:
    # The base class refuses all addressed delivery. Without an override,
    # every cron result and notify-tool message to Signal is dropped.

    @pytest.mark.asyncio
    async def test_note_to_self_is_allowed(self):
        assert (await _signal().authorize_outbound(OWN)).allowed

    @pytest.mark.asyncio
    async def test_unlisted_number_is_refused(self):
        assert not (await _signal().authorize_outbound(FRIEND)).allowed

    @pytest.mark.asyncio
    async def test_listed_number_is_allowed(self):
        assert (await _signal(outbound=[FRIEND]).authorize_outbound(FRIEND)).allowed


class TestSignalStop:
    @pytest.mark.parametrize("turn, expected", [
        ("stopped", "Stopped."),
        ("idle", "Nothing was running."),
        ("needs_confirmation", "without stopping its 1 background agent"),
    ])
    @pytest.mark.asyncio
    async def test_stop_reply(self, turn, expected):
        router = SimpleNamespace(
            get_last_session=AsyncMock(return_value="sess1"),
            stop_turn=AsyncMock(return_value={"turn": turn, "would_end": ["ag_1"]}),
        )
        ch = _signal(router=router)
        ch.send = AsyncMock()

        await ch._handle_stop(OWN)

        router.stop_turn.assert_awaited_once_with("sess1")
        assert expected in ch.send.await_args.args[0].text

    @pytest.mark.asyncio
    async def test_no_session(self):
        router = SimpleNamespace(
            get_last_session=AsyncMock(return_value=None), stop_turn=AsyncMock(),
        )
        ch = _signal(router=router)
        ch.send = AsyncMock()

        await ch._handle_stop(OWN)

        router.stop_turn.assert_not_called()
        assert "Nothing running" in ch.send.await_args.args[0].text


# --------------------------------------------------------------------------- #
#  /health/activity — the deployer's idle check                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("running, live, expected", [
    (set(), False, 0),
    ({"a", "b"}, False, 2),
    # A background agent outlives its turn and a restart kills it too.
    (set(), True, 1),
])
def test_health_activity(monkeypatch, running, live, expected):
    from nerve.gateway import server

    engine = SimpleNamespace(
        sessions=SimpleNamespace(get_running_ids=lambda: running),
        agents=SimpleNamespace(any_live=lambda: live),
    )
    monkeypatch.setattr(server, "_engine", engine)
    app = server.create_app()
    # No lifespan: this route reads only _engine.
    resp = TestClient(app).get("/health/activity")
    assert resp.status_code == 200
    assert resp.json() == {"running": expected}
