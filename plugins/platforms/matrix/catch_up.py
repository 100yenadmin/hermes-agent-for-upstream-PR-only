"""Opt-in Matrix startup replay; importing this module does not require mautrix."""
import json
import logging
import os
import time

logger = logging.getLogger("plugins.platforms.matrix.adapter")


def persistent_sync_store(store_dir, user_id):
    """Create a MemorySyncStore that checkpoints before the adapter dispatches a batch."""
    from mautrix.client.state_store import MemorySyncStore

    class PersistentSyncStore(MemorySyncStore):
        """Checkpoint token and saved_at before dispatch, without blocking sync on I/O failure."""
        _checkpoint_failed = False

        async def put_next_batch(self, token):
            if token == await self.get_next_batch():
                return
            path = store_dir / "sync_token.json"
            temporary = path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps({
                    "user_id": user_id, "next_batch": token, "saved_at": time.time()}), encoding="utf-8")
                os.replace(temporary, path)
            except OSError:
                if not self._checkpoint_failed:
                    logger.warning("Matrix: could not save catch-up checkpoint %s; catch-up after a restart may miss messages", path)
                self._checkpoint_failed = True
            else:
                self._checkpoint_failed = False
            await super().put_next_batch(token)

    return PersistentSyncStore()


def load_since(store_dir, user_id):
    """Return the account's token and numeric saved_at, ignoring absent or invalid checkpoints."""
    try:
        data = json.loads((store_dir / "sync_token.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    token = data.get("next_batch") if isinstance(data, dict) and data.get("user_id") == user_id else None
    saved_at = data.get("saved_at") if isinstance(data, dict) else None
    if isinstance(token, str) and token and type(saved_at) in (int, float):
        return token, saved_at
    return None


def resolve_catch_up_window(extra) -> int:
    try:
        return max(0, min(86400, int(extra.get("catch_up_window_seconds", 0))))
    except (TypeError, ValueError, OverflowError):
        logger.warning("Matrix: invalid catch_up_window_seconds; using 0")
        return 0


def select_catch_up_ids(sync_data, startup_ts, window, cap=20, *, saved_at, user_id) -> set[str]:
    """Guard replay by window, saved_at, own-join rooms, and per-room/overall caps."""
    candidates = []
    limited = []
    overflow = False
    oldest = max(startup_ts - window, saved_at - 5)
    for room, data in sync_data.get("rooms", {}).get("join", {}).items():
        timeline = data.get("timeline", {})
        if timeline.get("limited"):
            limited.append(room)
        events = timeline.get("events", [])
        if any(event.get("type") == "m.room.member" and event.get("state_key") == user_id
               and event.get("content", {}).get("membership") == "join" for event in events):
            continue
        room_candidates = []
        for event in events:
            timestamp = event.get("origin_server_ts", 0) / 1000
            if (event.get("type") in ("m.room.message", "m.room.encrypted") and event.get("event_id")
                    and event.get("sender") != user_id and oldest <= timestamp < startup_ts - 5):
                room_candidates.append((timestamp, event["event_id"]))
        overflow = overflow or len(room_candidates) > cap
        candidates.extend(sorted(room_candidates, reverse=True)[:cap])
    if limited:
        logger.info("Matrix: limited catch-up timelines (gaps not recovered): %s", ", ".join(limited))
    if overflow or len(candidates) > 100:
        logger.info("Matrix: catch-up overflow; keeping newest %d per room and 100 overall", cap)
    return {event_id for _, event_id in sorted(candidates, reverse=True)[:100]}


class MatrixCatchUpMixin:
    async def _catch_up_initial_sync(self, client):
        since = load_since(self._store_dir, self._user_id)
        if since:
            try:
                token, saved_at = since
                data = await client.sync(since=token, timeout=10000, full_state=True)
                if not isinstance(data, dict):
                    raise ValueError("unexpected sync response type")
                # Incremental sync omits quiet rooms, even with full_state=True.
                self._joined_rooms.clear()
                self._joined_rooms.update(await client.get_joined_rooms())
            except Exception:
                logger.warning("Matrix: catch-up sync failed; falling back to initial sync", exc_info=True)
            else:
                self._catch_up_ids = select_catch_up_ids(
                    data, self._startup_ts, self._catch_up_window, saved_at=saved_at, user_id=self._user_id)
                return data
        self._joined_rooms.clear()
        return await client.sync(timeout=10000, full_state=True)

    def _reset_clock_skew_detector(self) -> None:
        """State for _note_late_grace_drop: consecutive-drop count, their skew, and the once-only warning."""
        # Clock-skew detection: count grace-check drops that happen well after startup (i.e. not
        # initial-sync backfill). If the host's system clock is set ahead of real time, the startup grace
        # check `event_ts < startup_ts - 5` silently drops every live message. See #12614 — the symptom is
        # "bot joins rooms but never replies". Drops only count when their skew matches the first sampled
        # drop (within 60s), so varied-age backfill from freshly-invited rooms doesn't trip the heuristic.
        self._late_grace_drops: int = 0
        self._late_grace_skew: float = 0.0
        self._clock_skew_warned: bool = False

    def _note_late_grace_drop(self, event_ts: float) -> None:
        """Clock-skew heuristic for grace-check drops well after startup. A host clock set ahead of
        real time makes every live event look "older than startup" and the bot silently never
        replies. Warn once when drops keep happening >30s after startup with a *consistent* skew —
        unlike backfill from a freshly invited room, whose event ages vary widely and reset the counter."""
        if self._clock_skew_warned or time.time() - self._startup_ts <= 30:
            return
        skew = self._startup_ts - event_ts
        if not (5 < skew < 86400):  # ignore malformed/absurd timestamps
            return
        if self._late_grace_drops and abs(skew - self._late_grace_skew) < 60:
            self._late_grace_drops += 1
        else:
            self._late_grace_skew = skew
            self._late_grace_drops = 1
        if self._late_grace_drops >= 3:
            logger.warning(
                "Matrix: dropped %d consecutive live events as 'too old' more than 30s after startup "
                "(skew ≈ %.0fs). The host system clock is likely set ahead of real time, which causes "
                "the startup grace filter to silently discard every incoming message. Run "
                "`timedatectl set-ntp true` (or sync NTP) and restart the bot.", self._late_grace_drops, skew)
            self._clock_skew_warned = True
