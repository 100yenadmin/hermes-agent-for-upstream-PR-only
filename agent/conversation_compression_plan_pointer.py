"""Backend-independent active plan pointer at the compaction boundary."""

import json
import posixpath
import re

from agent.plan_prompt import PLAN_PROMPT_HEADER

PLAN_POINTER_HEADER = "[Active plan from /plan: "
_POINTER_SUFFIX = ". Re-read it before continuing the planned work.]"
_POINTER_LINE = re.compile(
    r"^" + re.escape(PLAN_POINTER_HEADER) + r"(.+\.md)" + re.escape(_POINTER_SUFFIX) + r"$",
    re.MULTILINE,
)


def _fold_plan_pointer(agent, messages: list, compressed: list) -> None:
    """Refresh one pointer from the pre-compaction history without reading the file."""
    from agent.context_compressor import _append_text_to_content, _extract_tool_call_name_and_args
    from agent.conversation_compression import _message_text, _replace_message_content
    from tools.todo_tool import TODO_INJECTION_HEADER

    paths = [(i, match) for i, row in enumerate(messages) if row.get("role") == "user"
             for match in _POINTER_LINE.findall(_message_text(row))]
    fallback = [match for row in compressed if row.get("role") == "user"
                for match in _POINTER_LINE.findall(_message_text(row))]
    pointer_index, path = paths[-1] if paths else (-1, fallback[-1] if fallback else None)
    plan_index = next((i for i in range(len(messages) - 1, -1, -1)
                       if messages[i].get("role") == "user"
                       and _message_text(messages[i]).startswith(PLAN_PROMPT_HEADER)), None)
    if plan_index is not None:
        start = max(plan_index, pointer_index) + 1
        calls = (call for row in reversed(messages[start:]) if row.get("role") == "assistant"
                 for call in reversed(row.get("tool_calls") or []))
        for call in calls:
            name, raw_args = _extract_tool_call_name_and_args(call)
            if name != "write_file":
                continue
            try:
                args = json.loads(raw_args)
            except (ValueError, TypeError):
                continue
            candidate = args.get("path") if isinstance(args, dict) else None
            if isinstance(candidate, str) and re.search(
                r"(?:^|/)\.hermes/plans/[^\r\n]+\.md\Z", posixpath.normpath(candidate.replace("\\", "/"))
            ):
                path = candidate
                break
    if path is None:
        return

    removed = False
    for i in range(len(compressed) - 1, -1, -1):
        row = compressed[i]
        if row.get("role") != "user" or not _POINTER_LINE.search(_message_text(row)):
            continue
        content = row.get("content")
        if isinstance(content, str):
            cleaned = _POINTER_LINE.sub("", content).strip()
        elif isinstance(content, list):
            cleaned = [{**part, "text": _POINTER_LINE.sub("", part["text"]).strip()}
                       if part.get("type") == "text" else part for part in content]
            cleaned = [part for part in cleaned if part.get("type") != "text" or part["text"]]
        else:
            continue
        if cleaned == content:
            continue
        if not cleaned and row.get("_plan_pointer_synthetic"):
            compressed.pop(i)
            removed = True
        else:
            _replace_message_content(row, cleaned)
            row.pop("_plan_pointer_synthetic", None)
    if removed:
        agent._repair_message_sequence(compressed)

    pointer = f"{PLAN_POINTER_HEADER}{path}{_POINTER_SUFFIX}"
    if compressed and compressed[-1].get("role") == "user":
        tail = compressed[-1]
        content = tail.get("content") or ""
        # Text todos consume their entire trailing block; structured todos only consume their own part.
        if isinstance(content, str):
            before, marker, after = content.partition(TODO_INJECTION_HEADER)
            content = f"{before.rstrip()}\n\n{pointer}\n\n{marker}{after}".strip()
        else:
            content = _append_text_to_content(content, pointer)
        _replace_message_content(tail, content)
    else:
        compressed.append({"role": "user", "content": pointer, "_plan_pointer_synthetic": True})
