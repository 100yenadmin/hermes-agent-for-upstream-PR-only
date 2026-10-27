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
        async def put_next_batch(self, token):
            if token == await self.get_next_batch():
                return
            path = store_dir / "sync_token.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"user_id": user_id, "next_batch": token}), encoding="utf-8")
            os.replace(temporary, path)
            await super().put_next_batch(token)

    return PersistentSyncStore()


def load_since(store_dir, user_id):
    """Ignore absent, corrupt, or different-account checkpoints."""
    try:
        data = json.loads((store_dir / "sync_token.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    token = data.get("next_batch") if isinstance(data, dict) and data.get("user_id") == user_id else None
    return token if isinstance(token, str) and token else None


def resolve_catch_up_window(extra) -> int:
    try:
        return max(0, min(86400, int(extra.get("catch_up_window_seconds", 0))))
    except (TypeError, ValueError, OverflowError):
        logger.warning("Matrix: invalid catch_up_window_seconds; using 0")
        return 0


def select_catch_up_ids(sync_data, startup_ts, window, cap=20) -> set[str]:
    candidates = []
    limited = []
    for room, data in sync_data.get("rooms", {}).get("join", {}).items():
        timeline = data.get("timeline", {})
        if timeline.get("limited"):
            limited.append(room)
        for event in timeline.get("events", []):
            timestamp = event.get("origin_server_ts", 0) / 1000
            if (event.get("type") in ("m.room.message", "m.room.encrypted") and event.get("event_id")
                    and startup_ts - window <= timestamp < startup_ts - 5):
                candidates.append((timestamp, event["event_id"]))
    if limited:
        logger.info("Matrix: limited catch-up timelines (gaps not recovered): %s", ", ".join(limited))
    if len(candidates) > cap:
        logger.info("Matrix: catch-up overflow; keeping newest %d of %d events", cap, len(candidates))
    return {event_id for _, event_id in sorted(candidates, reverse=True)[:cap]}


class MatrixCatchUpMixin:
    async def _catch_up_initial_sync(self, client):
        since = load_since(self._store_dir, self._user_id)
        if since:
            try:
                data = await client.sync(since=since, timeout=10000, full_state=True)
                if not isinstance(data, dict):
                    raise ValueError("unexpected sync response type")
                # Incremental sync omits quiet rooms, even with full_state=True.
                self._joined_rooms.update(await client.get_joined_rooms())
            except Exception:
                logger.warning("Matrix: catch-up sync failed; falling back to initial sync", exc_info=True)
            else:
                self._catch_up_ids = select_catch_up_ids(data, self._startup_ts, self._catch_up_window)
                return data
        self._joined_rooms.clear()
        return await client.sync(timeout=10000, full_state=True)

    def _reset_clock_skew_detector(self) -> None:
        """Reset per-connect clock-skew warning state."""
        self._late_grace_drops: int = 0
        self._late_grace_skew: float = 0.0
        self._clock_skew_warned: bool = False

    def _note_late_grace_drop(self, event_ts: float) -> None:
        """Warn on consistent late skew, excluding initial or varied-age room backfill."""
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
                "Matrix: dropped %d consecutive live events as 'too old' more than 30s after startup (skew ≈ %.0fs). "
                "The host system clock is likely set ahead of real time, which causes the startup grace filter to silently "
                "discard every incoming message. Run `timedatectl set-ntp true` (or sync NTP) and restart the bot.",
                self._late_grace_drops, skew)
            self._clock_skew_warned = True
