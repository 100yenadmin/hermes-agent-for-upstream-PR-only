"""Exercise the registered terminal and process tools with a real foreground job.

Run from the Hermes source tree using its Python and an isolated HERMES_HOME.
Exit 1 if the tool promises a notification its actual process cannot send.
"""
import json
import os
import shlex
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from gateway.session_context import async_delivery_supported, declare_stateless_channel
from tools import terminal_tool  # registers the tool
from tools.interrupt import request_yield
from tools.process_registry import process_registry
from tools.registry import registry


def dispatch(name, arguments, owner):
    return json.loads(registry.dispatch(name, arguments, task_id=owner))


def check(root, kind, test_passes):
    case = tempfile.TemporaryDirectory(dir=root)
    with case as case_dir:
        work = Path(case_dir)
        expected = 4 if test_passes else 5
        (work / "test_example.py").write_text(
            f"import unittest\nclass Example(unittest.TestCase):\n"
            f"    def test_total(self):\n        self.assertEqual(2 + 2, {expected})\n"
        )
        # The file barrier lets us redirect a genuinely running process, regardless of CPU load.
        (work / "check.py").write_text(
            "import pathlib, sys, time, unittest\n"
            "with open('starts', 'a') as log: print('started', file=log)\n"
            "deadline = time.monotonic() + 15\n"
            "while not pathlib.Path('release').exists():\n"
            "    if time.monotonic() > deadline: sys.exit(3)\n"
            "    time.sleep(0.03)\n"
            "unittest.main(module='test_example', argv=['check'])\n"
        )
        owner = f"fixture-{kind}-{expected}"
        reply = {}

        def call():
            if kind == "stateless":
                declare_stateless_channel()
            assert async_delivery_supported() == (kind == "interactive")
            reply.update(dispatch("terminal", {
                "command": f"{shlex.quote(sys.executable)} check.py",
                "workdir": str(work), "timeout": 25,
            }, owner))

        worker = threading.Thread(target=call)
        worker.start()
        session_id = None
        try:
            limit = time.monotonic() + 15
            gate = threading.Event()
            while not (work / "starts").exists() and worker.is_alive() and time.monotonic() < limit:
                gate.wait(0.025)
            assert (work / "starts").exists(), f"foreground command did not start: {reply}"
            request_yield(worker.ident)
            worker.join(timeout=7)
            assert not worker.is_alive() and reply["status"] == "yielded_to_background", reply
            session_id = reply["session_id"]
            actual = process_registry.get(session_id)
            assert actual is not None
            armed = actual.notify_on_complete
            (work / "release").touch()
            result = dispatch("process_manage", {
                "action": "wait", "session_id": session_id, "timeout": 8,
            }, owner)
            unit_result = "OK" if test_passes else "FAILED (failures=1)"
            assert result["status"] == "exited" and result["exit_code"] == int(not test_passes), result
            assert unit_result in result["output"] and (work / "starts").read_text().splitlines() == ["started"]
            row = dict(channel=kind, job_exit=result["exit_code"], starts=1, watcher_armed=armed,
                       reported_notify=reply["notify_on_complete"],
                       promises_notice="you will be notified" in reply.get("note", "").lower(),
                       tells_to_poll=bool(reply.get("notify_unsupported")))
            print(json.dumps(row, sort_keys=True), flush=True)
            return (row["reported_notify"] == armed == (kind == "interactive")
                    and (kind == "interactive" or row["tells_to_poll"] and not row["promises_notice"]))
        finally:
            (work / "release").touch()
            worker.join(timeout=8)
            if session_id and process_registry.poll(session_id).get("status") == "running":
                process_registry.kill_process(session_id)


if __name__ == "__main__":
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    outcomes = [check(home, channel, ok) for channel, ok in
                (("stateless", True), ("stateless", False), ("interactive", True))]
    sys.exit(0 if all(outcomes) else 1)
