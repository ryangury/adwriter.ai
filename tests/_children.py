"""Shared stub for orchestrator tests: stands in for step_watchdog.run_watched,
so no child process or browser starts. on_child(kind, store) returns
(status, result-dict); the result dict is written where ctr_child.py would
write it."""
import json


def fake_run_watched(on_child):
    calls = []

    def run(make_argv, *, step, progress_path, **kw):
        argv = make_argv([], [])
        kind = argv[3]
        store = argv[argv.index("--store") + 1] if "--store" in argv else None
        status, result = on_child(kind, store)
        with open(argv[argv.index("--result") + 1], "w", encoding="utf-8") as f:
            json.dump(result, f)
        calls.append((kind, store, status))
        reason = {"ok": None,
                  "hung": f"{step} hung: no progress for 10 minutes",
                  "budget": f"{step} ran out of time after 126 of 136 vehicles (65-minute budget; it was still making progress)",
                  }.get(status, f"{step} {status}: simulated")
        return {"status": status, "returncode": 0 if status == "ok" else None, "restarts": 0, "skipped": [],
                "reason": reason, "progress": {"done": 126 if status != "ok" else 3, "total": 136 if status != "ok" else 3,
                                               "current": None}}

    run.calls = calls
    return run


def ok_children(kind, store):
    if kind == "durham":
        return "ok", {"counts": {"recorded": 0, "aborted": None}, "errors": [], "aborted": None}
    return "ok", {"counts": {store: {"attempted": 0, "recorded": 0, "failed": 0}} if store else {}, "errors": [],
                  "aborted": None}


def fake_recorded_today(per_step=126):
    """Stand-in for ctr_database.recorded_today: 0 before a child runs, `per_step`
    after it (one more step's worth each time the same store is asked twice)."""
    n = {}

    def f(store):
        k = n.get(store, 0)
        n[store] = k + 1
        return 0 if k % 2 == 0 else per_step

    return f
