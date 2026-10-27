"""Thread-safe evidence of attempts versus real tool dispatch within one turn."""
from functools import wraps
import threading

_lock = threading.Lock()


def reset(agent):
    with _lock:
        agent._turn_tool_tally = {"attempted": 0, "executed": 0, "refused": []}


def note_attempt(agent, key="attempted"):
    with _lock:
        if (tally := getattr(agent, "_turn_tool_tally", None)) is not None:
            tally[key] += 1


def note_executed(agent):
    note_attempt(agent, "executed")


def note_refused(agent, name):
    with _lock:
        if (tally := getattr(agent, "_turn_tool_tally", None)) is not None:
            if name not in tally["refused"] and len(tally["refused"]) < 5:
                tally["refused"].append(name)


def export(agent, result):
    with _lock:
        tally = getattr(agent, "_turn_tool_tally", None)
        if isinstance(result, dict) and tally is not None and tally["attempted"] > 0:
            result.update(tool_calls_attempted=tally["attempted"], tool_calls_executed=tally["executed"],
                          tool_calls_refused_names=list(tally["refused"]))


def track_attempt(run):
    """Observe the shared middleware entry/exit without growing the executor."""
    @wraps(run)
    def tracked(agent, **kwargs):
        note_attempt(agent)
        state = run(agent, **kwargs)
        if state.blocked or not state.dispatched:
            note_refused(agent, kwargs["function_name"])
        return state
    return tracked
