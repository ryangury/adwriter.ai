"""Network guard for the test runner (tests/run_tests.py puts this directory on
PYTHONPATH, so Python imports it at startup in every test process).

With ADWRITER_NETGUARD=1 and ADWRITER_ALLOW_NETWORK unset, every outbound
connection, connect_ex and DNS lookup raises NetworkBlocked and is recorded in
ADWRITER_NETGUARD_LOG. The runner fails a test whose log is not empty, even
when the code under test caught the error (towing.lookup turns a connection
error into "lookup unavailable" and carries on — that is how three paid tow
lookups slipped through an "offline" test run on 2026-10-05).
"""
import json
import os
import socket
import traceback

if os.environ.get("ADWRITER_NETGUARD") == "1" and os.environ.get("ADWRITER_ALLOW_NETWORK") != "1":

    class NetworkBlocked(OSError):
        pass

    _log = os.environ.get("ADWRITER_NETGUARD_LOG")

    def _record(what, target):
        if not _log:
            return
        try:
            with open(_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "what": what,
                    "target": repr(target)[:200],
                    "stack": "".join(traceback.format_stack(limit=12)[:-2]),
                }) + "\n")
        except OSError:
            pass

    def _blocked(what, target_index):
        def fn(*args, **kwargs):
            target = args[target_index] if len(args) > target_index else kwargs
            _record(what, target)
            raise NetworkBlocked(f"network blocked in tests: {what} {target!r}")
        return fn

    socket.socket.connect = _blocked("socket.connect", 1)
    socket.socket.connect_ex = _blocked("socket.connect_ex", 1)
    socket.create_connection = _blocked("socket.create_connection", 0)
    socket.getaddrinfo = _blocked("socket.getaddrinfo", 0)
    socket.NetworkBlocked = NetworkBlocked
