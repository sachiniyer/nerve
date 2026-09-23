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

*And the sender is NOT enough to tell who a message is for.* This is the
subtle one, and it was a real bug. A linked device syncs EVERY message the
operator sends from their phone — to their partner, to a group, to anyone —
as ``syncMessage.sentMessage`` with ``source`` set to the operator's own
number. An allowlist that checks only ``source`` therefore passes all of them,
and the agent treats every text the operator sends to another human as a
prompt addressed to itself. What distinguishes Note to Self is the
DESTINATION: only there does it equal the account's own number.

So this channel routes on the conversation, not the sender. Note to Self is
the only thing that reaches the agent. Everything else is recorded as history
the agent can read on request, and is never answered. See ``_classify``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import OrderedDict, deque
from pathlib import Path
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

# Words that mean "stop what you are doing", matched before the router sees
# them. Kept deliberately short and unambiguous: this has to be typeable
# one-handed by someone who has just realised the agent is deleting the wrong
# thing.
_STOP_WORDS = {"/stop", "stop", "stop!", "halt", "cancel", "abort"}

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

    # ------------------------------------------------------------------ #
    #  Classification — which conversation is this?                        #
    # ------------------------------------------------------------------ #

    def _own_number(self) -> str:
        return (self._cfg().number or "").strip()

    def _classify(self, envelope: dict) -> tuple[str, str, str, Any]:
        """Return ``(kind, peer, text, timestamp)``.

        ``kind`` is one of:

        * ``note_to_self`` — the operator talking TO THE AGENT. The only kind
          that is answered.
        * ``incoming``     — somebody else messaging the operator.
        * ``outgoing``     — the operator messaging somebody else, synced here
          from their phone.
        * ``echo``         — this channel's own reply coming back.
        * ``ignore``       — receipts, typing, read states, empty bodies.

        ``peer`` is the other party in the conversation, which is the sender
        for ``incoming`` and the destination for ``outgoing``.
        """
        source = envelope.get("source") or envelope.get("sourceNumber") or ""
        own = self._own_number()

        data = envelope.get("dataMessage")
        if isinstance(data, dict):
            text = data.get("message") or ""
            ts = envelope.get("timestamp")
            if not text:
                return ("ignore", source, "", ts)
            # A dataMessage from our own number is Note to Self arriving the
            # other way round on some client versions; treat it as such.
            if source and own and source == own and not data.get("groupInfo"):
                return ("note_to_self", source, text, ts)
            return ("incoming", source, text, ts)

        sync = envelope.get("syncMessage") or {}
        sent = sync.get("sentMessage")
        if isinstance(sent, dict):
            ts = sent.get("timestamp")
            if ts is not None and ts in self._sent_timestamps:
                return ("echo", source, "", ts)
            text = sent.get("message") or ""
            if not text:
                return ("ignore", source, "", ts)

            # A group message has no destination; it is never Note to Self.
            group = sent.get("groupInfo") or sent.get("groupV2")
            dest = (sent.get("destinationNumber") or sent.get("destination")
                    or "")
            if group:
                gid = ""
                if isinstance(group, dict):
                    gid = str(group.get("groupId") or group.get("id") or "")
                return ("outgoing", f"group:{gid}" if gid else "group", text, ts)
            if own and dest and dest == own:
                return ("note_to_self", own, text, ts)
            if not dest:
                # Unknown shape. Log it once at debug rather than guessing it
                # is for us — guessing wrong here is what caused the original
                # bug, and the fail-safe direction is "not addressed to me".
                logger.debug("signal: sentMessage with no destination: %s",
                             sorted(sent.keys()))
                return ("ignore", source, "", ts)
            return ("outgoing", dest, text, ts)

        return ("ignore", source, "", envelope.get("timestamp"))

    # ------------------------------------------------------------------ #
    #  History — readable, never answered                                  #
    # ------------------------------------------------------------------ #

    def _history_db(self) -> Path:
        # Through nerve.paths, like every other machine-local file, so a
        # NERVE_HOME override is honoured everywhere at once.
        from nerve.paths import nerve_path

        return nerve_path("signal-history.db")

    def _record(self, kind: str, peer: str, text: str, ts: Any) -> None:
        """Append a non-agent message to the readable history.

        Sachin asked that the agent be able to READ his other Signal
        conversations while only ever talking to him in Note to Self, so those
        messages are stored rather than dropped. This is a plain local SQLite
        file with no index and no LLM anywhere near it — the `signal` skill
        greps it.

        Best-effort on purpose: history is a convenience, and a write failure
        must never break message handling.
        """
        try:
            db = self._history_db()
            db.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(db), timeout=5)
            try:
                con.execute(
                    "create table if not exists messages ("
                    "ts integer, direction text, peer text, peer_name text, "
                    "body text, recorded_at integer, "
                    "primary key (ts, direction, peer))"
                )
                con.execute(
                    "insert or ignore into messages values (?,?,?,?,?,?)",
                    (int(ts) if ts is not None else int(time.time() * 1000),
                     kind, peer, "", text, int(time.time())),
                )
                con.commit()
            finally:
                con.close()
        except Exception as e:
            logger.debug("signal: could not record history: %s", e)

    async def _handle_raw(self, raw: str | bytes) -> None:
        payload = json.loads(raw)
        envelope = payload.get("envelope") or {}

        kind, peer, text, ts = self._classify(envelope)
        if kind in ("echo", "ignore"):
            return

        # Everything that is not the operator talking to the agent is recorded
        # and then dropped. This is the fix for the bug in the module
        # docstring: these used to reach the router and be answered.
        if kind != "note_to_self":
            self._record(kind, peer, text, ts)
            logger.debug("signal: recorded %s message with %s (%d chars)",
                         kind, peer, len(text))
            return

        source = peer or self._own_number()

        if not self._is_allowed(source):
            logger.warning("signal: ignoring message from non-allowlisted %s", source)
            return

        # Intercept BEFORE the router. A normal message is queued behind the
        # running turn, so a "stop" sent through the usual path would not be
        # read until the thing it is trying to stop had already finished —
        # which makes it useless exactly when it matters.
        if text.strip().lower() in _STOP_WORDS:
            await self._handle_stop(source)
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
                # The router reads "message_id" to enable reactions
                # (ChannelRouter._message_context). For Signal the message id
                # IS the envelope timestamp — set both keys: under the name
                # the router looks for, and under the name that describes what
                # it is. Declaring the REACTIONS capability without this key
                # leaves the capability advertised and unusable.
                "message_id": ts,
                "timestamp": ts,
            },
        )
        await self.router.handle_message(msg)

    async def _handle_stop(self, source: str) -> None:
        """Interrupt the running turn for this conversation.

        This CANCELS in-flight work — it is the escape hatch for "you are
        deleting the wrong messages", not a way to add a note mid-task. The
        SDK interrupt ends the turn gracefully and keeps the client alive, so
        the next message continues the same conversation rather than starting
        a new one.

        Always replies, including when there was nothing to stop: silence here
        reads as "the stop did not arrive", which is the worst possible
        ambiguity in the moment someone sends it.
        """
        channel_key = f"signal:{source}"
        try:
            session_id = await self.router.engine.sessions.get_last_session(channel_key)
        except Exception as e:
            logger.error("signal: could not resolve session for stop: %s", e)
            session_id = None

        if not session_id:
            await self.send(OutboundMessage(target=source,
                                            text="Nothing running to stop."))
            return

        try:
            stopped = await self.router.engine.stop_session(session_id)
        except Exception as e:
            logger.error("signal: stop_session failed: %s", e, exc_info=True)
            await self.send(OutboundMessage(
                target=source,
                text=f"Could not stop — {type(e).__name__}. "
                     "If it is still going, scale the deployment to 0."))
            return

        logger.info("signal: stop requested for session %s -> %s", session_id, stopped)
        await self.send(OutboundMessage(
            target=source,
            text=("Stopped. Whatever was mid-flight is cancelled — anything it "
                  "already did stands. Tell me what to do next.")
            if stopped else
            "Nothing was running. Ready when you are."))

    # ------------------------------------------------------------------ #
    #  Send                                                                #
    # ------------------------------------------------------------------ #

    def _may_send_to(self, target: str) -> bool:
        """May the agent put a message into this conversation?

        Note to Self always. Anything else only if the number is listed in
        ``signal.outbound_allowed_numbers``, which is empty by default.

        This is enforced here, at the one place every outbound path funnels
        through, rather than by telling the agent not to. Reading someone's
        messages and writing into their thread are different powers: the agent
        is given the first freely and the second not at all, because a message
        sent under Sachin's name to another person cannot be recalled and does
        not look like it came from an agent.
        """
        own = self._own_number()
        t = (target or "").strip()
        if not t:
            return False
        if own and t == own:
            return True
        if t in (self._cfg().outbound_allowed_numbers or []):
            return True
        logger.warning(
            "signal: REFUSING to send to %s — not Note to Self and not in "
            "signal.outbound_allowed_numbers. Add the number there to allow "
            "it deliberately.", t,
        )
        return False

    async def send(self, message: OutboundMessage) -> None:
        if not self._client:
            logger.error("signal: send called before start()")
            return
        if not self._may_send_to(message.target):
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
        reply, so it is swallowed. Gated the same as send() — a typing
        indicator appearing in someone else's thread is itself a message.
        """
        if not self._client or not self._may_send_to(target):
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
        if not self._client or not self._may_send_to(target):
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

        if not self._client or not self._may_send_to(target):
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
