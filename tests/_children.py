"""Shared stub for orchestrator tests: stands in for step_watchdog.run_watched,
so no child process or browser starts. on_child(kind, store) returns
(status, result-dict); the result dict is written where ctr_child.py would
write it."""
import json


def fake_run_watched(on_child):
    calls = []

    def run(make_argv, *, step, progress_path, **kw):
        argv = make_argv([])
        kind = argv[3]
        store = argv[argv.index("--store") + 1] if "--store" in argv else None
        status, result = on_child(kind, store)
        with open(argv[argv.index("--result") + 1], "w", encoding="utf-8") as f:
            json.dump(result, f)
        calls.append((kind, store, status))
        return {"status": status, "returncode": 0 if status == "ok" else None, "restarts": 0, "skipped": [],
                "reason": None if status == "ok" else f"{step} {status}: simulated"}

    run.calls = calls
    return run


def ok_children(kind, store):
    if kind == "durham":
        return "ok", {"counts": {"recorded": 0, "aborted": None}, "errors": [], "aborted": None}
    return "ok", {"counts": {store: {"attempted": 0, "recorded": 0, "failed": 0}} if store else {}, "errors": [],
                  "aborted": None}
