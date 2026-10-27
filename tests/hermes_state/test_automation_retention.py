"""Content guards for automation retention, including the resumed-source regression (#110589)."""

import logging
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def seed(db, sid, source="cron", *, days=60, users=(0,), ended=True, parent=None):
    start = time.time() - days * 86400
    db.create_session(sid, source, parent_session_id=parent)
    for offset in users:
        db.append_message(sid, "user", "synthetic prompt", timestamp=start + offset)
    db.append_message(sid, "assistant", "synthetic result", timestamp=start + 90)
    if ended:
        db.end_session(sid, "done")
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET started_at=?, last_activity_at=?, ended_at=? WHERE id=?",
        (start, start + 100, start + 100 if ended else None, sid)))
    return start


def sweep(db, raw=None, **kwargs):
    return db.maybe_auto_prune_and_vacuum(
        source_retention_days=raw if raw is not None else {"cron": 30, "subagent": 30},
        global_prune=False, min_interval_hours=0, vacuum=False, **kwargs)


def test_source_only_retention_leaves_human_sources_and_recent_automation(db):
    for source in ("cron", "kanban", "subagent", "telegram", "cli", "desktop"):
        seed(db, source, source, days=200)
    seed(db, "recent", days=10)
    result = sweep(db, {"cron": 30, "kanban": 30, "subagent": 30})
    assert result["pruned"] == 3
    assert all(db.get_session(s) is None for s in ("cron", "kanban", "subagent"))
    assert all(db.get_session(s) is not None for s in ("telegram", "cli", "desktop", "recent"))


def test_human_keys_warn_once_and_never_delete(db, caplog):
    sources = ("cli", "telegram", "api_server", "recovered")
    for source in sources:
        seed(db, source, source)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        result = sweep(db, dict.fromkeys(sources, 1))
    assert result["pruned"] == result["closed"] == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == len(sources)
    for source in sources:
        assert db.get_session(source) is not None
        assert sum(repr(source) in line and "human history is never pruned" in line for line in warnings) == 1


@pytest.mark.parametrize("source", ["cron", "subagent"])
def test_real_resume_loader_preserves_human_continuation(db, monkeypatch, source):
    """Replays BLOCKED.md's loader/upsert proof without erasing the follow-up's later timestamp."""
    from hermes_cli.oneshot import _load_resume_target

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    start = seed(db, "continued", source, days=61)
    seed(db, "automation", source, days=61)
    resumed, history, _meta = _load_resume_target(db, "continued")
    assert resumed == "continued" and history
    db.create_session(resumed, "cli")
    assert db.get_session(resumed)["source"] == source
    assert db.get_session(resumed)["started_at"] == start
    db.append_message(resumed, "user", "synthetic human follow-up", timestamp=start + 86400)
    db.end_session(resumed, "done")
    result = sweep(db)
    assert result["pruned"] == 1
    assert db.get_session("automation") is None
    assert db.get_session(resumed) is not None
    assert db.get_messages(resumed)[-1]["content"] == "synthetic human follow-up"


@pytest.mark.parametrize("offsets,keep", [((601,), True), ((0, 30), True), ((600,), False), ((), False)])
def test_counts_all_user_rows_and_keeps_late_first_input(db, offsets, keep):
    seed(db, "candidate", users=offsets)
    # Compaction/inactive markers must not hide user evidence from the guard.
    db._execute_write(lambda c: c.execute(
        "UPDATE messages SET active=0, compacted=1, _compressed_summary=1 WHERE role='user'"))
    result = sweep(db)
    assert (db.get_session("candidate") is not None) is keep
    assert result["pruned"] == int(not keep)


@pytest.mark.parametrize("human_segment", ["root", "tip"])
def test_every_compression_segment_must_be_automation_only(db, human_segment):
    start = seed(db, "root", ended=False)
    if human_segment == "root":
        db.append_message("root", "user", "synthetic human continuation", timestamp=start + 86400)
    db.end_session("root", "compression")
    seed(db, "tip", parent="root")
    if human_segment == "tip":
        db.append_message("tip", "user", "synthetic human continuation", timestamp=start + 86400)
    assert sweep(db)["pruned"] == 0
    assert db.get_compression_lineage("root") == ["root", "tip"]


def test_pinned_lineage_and_live_turn_are_kept(db):
    seed(db, "pinned", ended=False)
    db.set_session_pinned("pinned", True)
    db.end_session("pinned", "compression")
    seed(db, "tip", parent="pinned")
    seed(db, "live", ended=False)
    assert db.try_acquire_session_turn_lease("live", "fixture-holder", ttl_seconds=300)
    assert sweep(db)["pruned"] == 0
    assert db.get_session("pinned") is not None
    assert db.get_session("tip") is not None
    assert db.get_session("live")["ended_at"] is None


def test_source_sweep_closes_then_waits_a_full_window(db):
    seed(db, "orphan", ended=False)
    seed(db, "cli-open", "cli", days=200, ended=False)
    first = sweep(db)
    assert first["closed"] == 1 and first["pruned"] == 0
    assert db.get_session("orphan")["end_reason"] == "startup_orphan_reap"
    assert db.get_session("cli-open")["ended_at"] is None
    assert sweep(db)["pruned"] == 0
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET ended_at=? WHERE id='orphan'", (time.time() - 31 * 86400,)))
    assert sweep(db)["pruned"] == 1


def test_deleting_subagent_never_touches_human_parent(db):
    seed(db, "parent", "telegram", days=200, users=(0, 60))
    seed(db, "child", "subagent", parent="parent")
    before = db.get_session("parent"), db.get_messages("parent")
    assert sweep(db)["pruned"] == 1
    assert db.get_session("child") is None
    assert (db.get_session("parent"), db.get_messages("parent")) == before


def test_source_only_deletions_reach_vacuum_and_log(db, monkeypatch, caplog):
    seed(db, "old")
    calls = []
    monkeypatch.setattr(db, "_freelist_ratio", lambda: 0.5)
    monkeypatch.setattr(db, "vacuum", lambda: calls.append(True))
    monkeypatch.setattr("hermes_state_holders.foreign_state_db_holders", lambda _: [])
    monkeypatch.setattr("hermes_state_holders.in_process_state_db_holders", lambda *a, **k: [])
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        result = db.maybe_auto_prune_and_vacuum(
            global_prune=False, source_retention_days={"cron": 30}, min_interval_hours=0)
    assert result["pruned"] == 1 and result["vacuumed"]
    assert calls == [True]
    assert "pruned 1 session(s)" in caplog.text and "cron" in caplog.text


@pytest.mark.parametrize("raw", [None, {}])
def test_empty_source_map_keeps_global_prune_and_sweep_behavior(db, raw):
    seed(db, "old", "telegram", days=200)
    seed(db, "young", "cron", days=60)
    seed(db, "orphan", "cli", days=200, ended=False)
    options = {} if raw is None else {"source_retention_days": raw}
    result = db.maybe_auto_prune_and_vacuum(
        retention_days=90, min_interval_hours=0, vacuum=False, **options)
    assert result == {"skipped": False, "pruned": 1, "closed": 1, "vacuumed": False}
    assert db.get_session("old") is None
    assert db.get_session("young") is not None
    assert db.get_session("orphan")["end_reason"] == "startup_orphan_reap"


def test_default_manual_prune_is_unchanged(db):
    seed(db, "human-in-cron", users=(0, 86400))
    assert db.prune_sessions(older_than_days=30, source="cron") == 1
