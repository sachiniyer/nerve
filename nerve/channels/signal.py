"""Signal channel — talks to signal-cli-rest-api over localhost HTTP.

Signal has no bot API. Every Signal integration anywhere is a wrapper around
signal-cli, an unofficial client; this channel delegates the protocol entirely
to `bbernhard/signal-cli-rest-api` running as a sidecar and only speaks HTTP to
it.

**The sidecar must run in MODE=json-rpc.** In MODE=normal the receive endpoint
is a polling route rather than a websocket, the upgrade fails *silently*, and
no inbound message ever arrives while everything still looks healthy.

Two things about this transport are not obvious and are easy to regress:

*Note to Self is the normal case.* The account is linked as a secondary device
on the operator's own number, so the operator talks to the agent by messaging
themselves. Those arrive as ``syncMessage.sentMessage``, not ``dataMessage``.
A channel that only handles ``dataMessage`` looks fine on a test from a second
phone and never responds to the person who actually owns it.

*Which means our own replies come back to us.* Everything this channel sends
is echoed back as a syncMessage moments later. Without suppression the agent
answers its own reply, forever. The send API returns the message timestamp, so
we remember what we sent and drop the echo (see ``_sent_timestamps``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from typing import Any, Callable

import httpx

from nerve.channels.base import (
    BaseChannel,
    ChannelCapability,
    ChannelConstraints,
    InboundMessage,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

# Signal's own limit is far higher, but long agent answers are unpleasant to
# read on a phone and Signal has no message edit to tidy them up with.
_MAX_MESSAGE_LENGTH = 4000

# How many of our own send-timestamps to remember for echo suppression. The
# echo arrives within seconds; this is orders of magnitude more headroom than
# needed and still bounded.
_SENT_MEMORY = 256

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0


class SignalChannel(BaseChannel):
    """Signal via signal-cli-rest-api.

    Receives over a websocket (``/v1/receive/<number>``) and sends over REST
    (``POST /v2/send``). Both are localhost-only in this deployment, so there
    is no auth between this process and the sidecar — the pod boundary is it.
    """

    def __init__(self, get_config: Callable[[], Any], router: Any) -> None:
        self._get_config = get_config
        self.router = router
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._client: httpx.AsyncClient | None = None
        # Timestamps of messages we sent, for echo suppression.
        self._sent_timestamps: deque[int] = deque(maxlen=_SENT_MEMORY)
        # Reacting to a message requires naming its ORIGINAL AUTHOR, not just
        # the conversation. The router only hands us a message id (a Signal
        # timestamp), so the author has to be remembered when the message
        # arrives or the reaction cannot be addressed. Bounded, same as above.
        self._inbound_authors: OrderedDict[int, str] = OrderedDict()

    # ------------------------------------------------------------------ #
    #  Identity                                                            #
    # ------------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return "signal"

    @property
    def capabilities(self) -> ChannelCapability:
        # Deliberately NOT STREAMING. Signal cannot edit a sent message, so
        # streaming would mean one new message per chunk — a notification
        # storm on the operator's phone for every answer.
        return (
            ChannelCapability.SEND_TEXT
            | ChannelCapability.MARKDOWN
            | ChannelCapability.SEND_FILES
            | ChannelCapability.TYPING_INDICATOR
            | ChannelCapability.REACTIONS
        )

    @property
    def constraints(self) -> ChannelConstraints:
        return ChannelConstraints(
            max_message_length=_MAX_MESSAGE_LENGTH,
            supports_message_edit=False,
        )

    # ------------------------------------------------------------------ #
    #  Config helpers                                                      #
    # ------------------------------------------------------------------ #

    def _cfg(self) -> Any:
        """Read config per use, so a reload is picked up without a restart."""
        return self._get_config().signal

    def _is_allowed(self, number: str) -> bool:
        """Allowlist check. Empty allowlist means nobody, never everybody.

        A Signal message becomes a tool call on this cluster. There is no
        "open" mode here by design, and the fail-safe direction on a missing
        or malformed allowlist is closed.
        """
        allowed = self._cfg().allowed_numbers
        if not allowed:
            logger.warning(
                "signal: allowed_numbers is empty — rejecting message from %s. "
                "Set signal.allowed_numbers in the workspace settings.yaml.",
                number,
            )
            return False
        return number in allowed

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        cfg = self._cfg()
        self._stopping = False
        self._client = httpx.AsyncClient(base_url=cfg.api_url, timeout=30.0)
        self._task = asyncio.create_task(self._receive_loop())
        logger.info(
            "Signal channel started (number=%s, api=%s, %d allowed sender(s))",
            cfg.number, cfg.api_url, len(cfg.allowed_numbers),
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Signal channel stopped")

    # ------------------------------------------------------------------ #
    #  Receive                                                             #
    # ------------------------------------------------------------------ #

    async def _receive_loop(self) -> None:
        """Hold the receive websocket open, reconnecting with backoff.

        The sidecar restarts independently of this process (it is a separate
        container), so a dropped connection is routine rather than fatal. The
        loop only exits when the channel is stopped.
        """
        import websockets

        cfg = self._cfg()
        ws_url = (
            cfg.api_url.replace("https://", "wss://").replace("http://", "ws://")
            + f"/v1/receive/{cfg.number}"
        )
        delay = _RECONNECT_MIN

        while not self._stopping:
            try:
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    logger.info("Signal receive websocket connected")
                    delay = _RECONNECT_MIN  # reset only after a real connect
                    async for raw in ws:
                        if self._stopping:
                            break
                        try:
                            await self._handle_raw(raw)
                        except Exception as e:
                            # One malformed envelope must not take the loop
                            # down and stop the agent answering.
                            logger.error(
                                "signal: error handling message: %s", e, exc_info=True
                            )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stopping:
                    break
                logger.warning(
                    "signal: receive websocket dropped (%s); reconnecting in %.0fs",
                    e, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)

    async def _handle_raw(self, raw: str | bytes) -> None:
        payload = json.loads(raw)
        envelope = payload.get("envelope") or {}
        source = envelope.get("source") or envelope.get("sourceNumber") or ""

        text, is_echo = self._extract_text(envelope)
        if is_echo or not text:
            return

        if not self._is_allowed(source):
            logger.warning("signal: ignoring message from non-allowlisted %s", source)
            return

        ts = envelope.get("timestamp")
        if ts is not None:
            try:
                self._inbound_authors[int(ts)] = source
                while len(self._inbound_authors) > _SENT_MEMORY:
                    self._inbound_authors.popitem(last=False)
            except (TypeError, ValueError):
                pass

        logger.info("signal: inbound from %s (%d chars)", source, len(text))

        msg = InboundMessage(
            channel_name="signal",
            channel_key=f"signal:{source}",
            # The router replies to sender_id, so this must be the number.
            sender_id=source,
            text=text,
            metadata={
                "source_name": envelope.get("sourceName", ""),
                "source_uuid": envelope.get("sourceUuid", ""),
                "timestamp": envelope.get("timestamp"),
            },
        )
        await self.router.handle_message(msg)

    def _extract_text(self, envelope: dict) -> tuple[str, bool]:
        """Pull the message body out of an envelope.

        Returns ``(text, is_echo)``. Handles both shapes:

        * ``dataMessage`` — someone else messaging the account.
        * ``syncMessage.sentMessage`` — a message sent from *any* device on
          this account, including the operator's phone (Note to Self) and
          including this channel's own replies.

        The second case is why echo suppression exists: our own sends come
        back here, and answering them would loop forever.
        """
        data = envelope.get("dataMessage")
        if isinstance(data, dict):
            return (data.get("message") or "", False)

        sync = envelope.get("syncMessage") or {}
        sent = sync.get("sentMessage")
        if isinstance(sent, dict):
            ts = sent.get("timestamp")
            if ts is not None and ts in self._sent_timestamps:
                return ("", True)  # our own reply, echoed back
            return (sent.get("message") or "", False)

        # Receipts, typing indicators, read states — nothing to act on.
        return ("", False)

    # ------------------------------------------------------------------ #
    #  Send                                                                #
    # ------------------------------------------------------------------ #

    async def send(self, message: OutboundMessage) -> None:
        if not self._client:
            logger.error("signal: send called before start()")
            return

        cfg = self._cfg()
        for chunk in _chunk(message.text, _MAX_MESSAGE_LENGTH):
            try:
                resp = await self._client.post(
                    "/v2/send",
                    json={
                        "number": cfg.number,
                        "recipients": [message.target],
                        "message": chunk,
                    },
                )
                resp.raise_for_status()
                # Remember the timestamp so the echo of this message is
                # recognised and dropped when it arrives back over the
                # receive websocket.
                ts = (resp.json() or {}).get("timestamp")
                if ts is not None:
                    try:
                        self._sent_timestamps.append(int(ts))
                    except (TypeError, ValueError):
                        pass
            except Exception as e:
                logger.error("signal: send to %s failed: %s", message.target, e)
                return

    async def send_typing(self, target: str) -> None:
        """Best-effort typing indicator.

        Purely cosmetic: a failure here must never interfere with the actual
        reply, so it is swallowed.
        """
        if not self._client:
            return
        try:
            await self._client.put(
                f"/v1/typing-indicator/{self._cfg().number}",
                json={"recipient": target},
            )
        except Exception:
            pass

    async def set_reaction(self, target: str, message_id: int, emoji: str) -> None:
        """React to a message with an emoji.

        Cheap feedback that costs no notification: 👀 on pickup, ✅ on done,
        rather than sending "working on it" as a whole message.

        ``message_id`` is the Signal timestamp of the message being reacted to.
        ``target_author`` must be whoever wrote it — looked up from what we saw
        arrive, falling back to the conversation target, which is correct for
        Note to Self and the common 1:1 case.

        Best-effort: a failed reaction must never interfere with the real
        reply, so nothing is raised.
        """
        if not self._client:
            return
        author = self._inbound_authors.get(int(message_id), target)
        try:
            resp = await self._client.post(
                f"/v1/reactions/{self._cfg().number}",
                json={
                    "reaction": emoji,
                    "recipient": target,
                    "target_author": author,
                    "timestamp": int(message_id),
                },
            )
            resp.raise_for_status()
        except Exception as e:
            logger.debug("signal: reaction %s on %s failed: %s", emoji, message_id, e)

    async def send_file(self, target: str, file_path: str) -> bool:
        import base64
        import os

        if not self._client:
            return False
        try:
            with open(file_path, "rb") as f:
                encoded = base64.b64encode(f.read()).decode()
            resp = await self._client.post(
                "/v2/send",
                json={
                    "number": self._cfg().number,
                    "recipients": [target],
                    "message": "",
                    "base64_attachments": [encoded],
                    "filename": os.path.basename(file_path),
                },
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error("signal: file send to %s failed: %s", target, e)
            return False

    def format_response(self, text: str) -> str:
        # Signal renders a useful subset of markdown natively; passing it
        # through unchanged is better than stripping it.
        return text


def _chunk(text: str, size: int) -> list[str]:
    """Split on paragraph then line boundaries before cutting mid-word.

    Signal cannot edit a message, so a badly split reply stays badly split.
    """
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        cut = window.rfind("\n\n")
        if cut < size // 2:
            cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut < size // 2:
            cut = size
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
