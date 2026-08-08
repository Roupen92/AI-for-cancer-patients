"""In-memory session + conversation registries for streaming events.

Two shapes live here:

* `Session` — one-shot consult (the /api/board endpoint and the eval harness).
  One queue, one result, done.
* `Conversation` — the chat. Holds the message history, the patient's profile,
  and crucially ONE `EvidenceLedger` for the whole conversation, so `[3]` means
  the same source in message 7 as it did in message 2. Each patient message
  becomes a `Turn` with its own event queue and its own SSE stream.

Everything is in-memory and TTL-reaped: nothing about a patient's health is
written to disk.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from app.evidence import EvidenceLedger

log = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 30 * 60
CONVERSATION_TTL_SECONDS = 2 * 60 * 60      # chats are longer-lived than one-shots
CLEANUP_INTERVAL_SECONDS = 60

# Bound on stored history per conversation. Old turns' text is dropped from the
# transcript when this is exceeded; the ledger keeps its labels either way.
MAX_MESSAGES_PER_CONVERSATION = 60


# --------------------------------------------------------------------------- #
# One-shot consult sessions
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    sid: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=512))
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    final_result: dict | None = None
    error: str | None = None
    is_streaming: bool = False
    # Lay summaries generated on-demand when the patient hovers a citation.
    # Cached so the same citation hovered twice doesn't pay the LLM cost twice.
    lay_summaries: dict[str, str] = field(default_factory=dict)


SESSIONS: dict[str, Session] = {}


def new_session() -> Session:
    sid = f"tb_{uuid.uuid4().hex[:12]}"
    s = Session(sid=sid)
    SESSIONS[sid] = s
    return s


def get(sid: str) -> Session | None:
    return SESSIONS.get(sid)


def emit_factory(session: Session):
    """Return a closure suitable for board.run_board's `emit` parameter."""
    def _emit(event_type: str, payload: dict) -> None:
        try:
            session.queue.put_nowait({"type": event_type, "payload": payload})
        except asyncio.QueueFull:
            # Drop silently if the client isn't draining fast enough.
            pass
    return _emit


# --------------------------------------------------------------------------- #
# Chat conversations
# --------------------------------------------------------------------------- #

@dataclass
class Turn:
    """One patient message and the work it kicked off.

    Events go into an append-only `events` log rather than a consumable queue, and
    readers track their own index. That makes the stream replayable: a client that
    connects a beat after POSTing, or reconnects after a dropped connection, reads
    from index 0 and sees the whole turn — no lost routing decision, no missing
    agent cards, and no queue to desync.
    """
    tid: str
    user_message: str
    events: list[dict] = field(default_factory=list)
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None

    def append(self, ev: dict) -> None:
        self.events.append(ev)
        self.wakeup.set()


@dataclass
class Conversation:
    cid: str
    profile: dict = field(default_factory=dict)
    # Which specialist's ROOM this conversation belongs to, "" for the "not sure
    # who to ask" front door. One conversation per room is what keeps each room's
    # evidence ledger labels stable and its history that specialist's history —
    # so app/server.py refuses a request that pins a different id against it.
    specialist: str = ""
    # Oldest-first: {"role": "user"|"assistant", "content": str, "turn_id": str,
    #               "references": [labels], "created_at": float}
    messages: list[dict] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    turns: dict[str, Turn] = field(default_factory=dict)
    lay_summaries: dict[str, str] = field(default_factory=dict)
    # Cached location parse + the raw string it came from, so a conversation pays
    # for location extraction once instead of once per turn.
    location_parsed: dict | None = None
    location_source: str = ""
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_active_at = time.time()

    def add_message(self, role: str, content: str, *, turn_id: str = "", references=None) -> dict:
        msg = {
            "role": role,
            "content": content,
            "turn_id": turn_id,
            "references": list(references or []),
            "created_at": time.time(),
        }
        self.messages.append(msg)
        if len(self.messages) > MAX_MESSAGES_PER_CONVERSATION:
            del self.messages[: len(self.messages) - MAX_MESSAGES_PER_CONVERSATION]
        self.touch()
        return msg

    def active_turns(self) -> int:
        return sum(1 for t in self.turns.values() if t.finished_at is None)


CONVERSATIONS: dict[str, Conversation] = {}


def new_conversation(profile: dict | None = None, specialist: str = "") -> Conversation:
    cid = f"cv_{uuid.uuid4().hex[:12]}"
    conv = Conversation(cid=cid, profile=dict(profile or {}), specialist=specialist or "")
    CONVERSATIONS[cid] = conv
    return conv


def get_conversation(cid: str) -> Conversation | None:
    conv = CONVERSATIONS.get(cid)
    if conv:
        conv.touch()
    return conv


def new_turn(conv: Conversation, user_message: str) -> Turn:
    tid = f"t_{uuid.uuid4().hex[:10]}"
    turn = Turn(tid=tid, user_message=user_message)
    conv.turns[tid] = turn
    conv.touch()
    return turn


# Soft cap on a turn's event log. Only `specialist_event` is ever dropped — it is
# a live progress line, so losing the 2000th one costs nothing, while dropping a
# routing decision or a result would break the turn.
MAX_TURN_EVENTS = 2000


def turn_emit_factory(turn: Turn):
    """Emit closure for a chat turn. Appends to the turn's replayable event log."""
    def _emit(event_type: str, payload: dict) -> None:
        if event_type == "specialist_event" and len(turn.events) >= MAX_TURN_EVENTS:
            return
        turn.append({"type": event_type, "payload": payload})
    return _emit


def total_active_turns() -> int:
    return sum(c.active_turns() for c in CONVERSATIONS.values())


# --------------------------------------------------------------------------- #
# Reaper
# --------------------------------------------------------------------------- #

async def cleanup_loop() -> None:
    while True:
        try:
            now = time.time()

            stale_sessions = [
                sid
                for sid, s in SESSIONS.items()
                if (s.finished_at and now - s.finished_at > SESSION_TTL_SECONDS)
                or (now - s.started_at > 2 * SESSION_TTL_SECONDS)
            ]
            for sid in stale_sessions:
                s = SESSIONS.pop(sid, None)
                if s and s.task and not s.task.done():
                    s.task.cancel()

            stale_convs = [
                cid
                for cid, c in CONVERSATIONS.items()
                if now - c.last_active_at > CONVERSATION_TTL_SECONDS
            ]
            for cid in stale_convs:
                c = CONVERSATIONS.pop(cid, None)
                if not c:
                    continue
                for t in c.turns.values():
                    if t.task and not t.task.done():
                        t.task.cancel()

            # Within a live conversation, drop the event logs of turns that
            # finished long ago — the transcript is what matters after that.
            for c in CONVERSATIONS.values():
                for t in list(c.turns.values()):
                    if t.finished_at and now - t.finished_at > SESSION_TTL_SECONDS:
                        t.events.clear()
                        c.turns.pop(t.tid, None)
        except asyncio.CancelledError:
            # Propagate cancellation so the lifespan shutdown completes cleanly.
            raise
        except Exception:
            log.exception("cleanup_loop iteration failed; continuing")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
