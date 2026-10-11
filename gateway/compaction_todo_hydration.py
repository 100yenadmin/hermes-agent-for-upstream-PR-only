"""Todo-list hydration for the gateway's detached compaction agents."""

import re
from typing import Any

from tools.todo_tool import TODO_INJECTION_HEADER

# One rendered line of TodoStore.format_for_injection: "<indent>- [x] <id>. <content> (<status>)".
_SNAPSHOT_ITEM = re.compile(r"^( *)- \[.\] (.+?)\. (.*)$")
_SNAPSHOT_STATUS = re.compile(r"^(.*) \((pending|in_progress|completed|cancelled)\)$", re.DOTALL)


def _row_text(msg: Any) -> str:
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        content = "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content if isinstance(content, str) else ""


def _is_todo_snapshot_row(msg: Any) -> bool:
    """True for a user row carrying a todo snapshot folded in by an earlier compaction."""
    return isinstance(msg, dict) and msg.get("role") == "user" and TODO_INJECTION_HEADER in _row_text(msg)


def _parse_todo_snapshot(text: str) -> list:
    """Read the items back out of a folded snapshot (the block after the header, up to any reload notice).

    Item content may itself hold blank lines, so only the notice the fold appends ends the block."""
    from agent.conversation_compression import _PRUNED_SKILL_RELOAD_NOTICE_HEADER

    block = text[text.find(TODO_INJECTION_HEADER) + len(TODO_INJECTION_HEADER):]
    block = block.split("\n\n" + _PRUNED_SKILL_RELOAD_NOTICE_HEADER, 1)[0].strip("\n")
    items: list = []
    parents: list = []  # parents[depth] = id of the latest item rendered at that depth
    for line in block.split("\n"):
        match = _SNAPSHOT_ITEM.match(line)
        if match is None:
            if items:  # multi-line content continues the previous item
                items[-1]["content"] += "\n" + line
            continue
        depth = len(match.group(1)) // 2
        del parents[depth:]
        item = {"id": match.group(2), "content": match.group(3)}
        if depth and len(parents) == depth:
            item["parent"] = parents[-1]
        parents.append(item["id"])
        items.append(item)
    for item in items:
        status = _SNAPSHOT_STATUS.match(item["content"])
        if status is not None:
            item["content"], item["status"] = status.group(1), status.group(2)
    return items


def hydrate_todos_for_compaction(agent: Any, messages: list) -> Any:
    """Return *agent* after replaying the transcript's latest todo state into its ``TodoStore``.

    Session hygiene and manual ``/compress`` compact with a freshly built agent whose store is empty.
    A live turn hydrates the store from history before it can compact; without the same replay the
    boundary has no task list to re-inject, and the list is lost once its tool rows are summarised.
    Only rows after the newest folded snapshot count: that snapshot is newer than any todo result
    before it (often one kept in the protected head), so replaying an older result would replace it.
    With no todo result after it, the snapshot itself is the latest state: restore its items so the
    boundary re-folds them even when the summariser drops the snapshot row.
    """
    hydrate = getattr(agent, "_hydrate_todo_store", None)
    if callable(hydrate):
        start = next((i + 1 for i in range(len(messages) - 1, -1, -1) if _is_todo_snapshot_row(messages[i])), 0)
        hydrate(messages[start:])
        latest = getattr(agent, "_latest_todo_response", None)
        if start and callable(latest) and latest(messages[start:]) is None:
            items = _parse_todo_snapshot(_row_text(messages[start - 1]))
            if items:
                agent._todo_store.restore(items)
    return agent
