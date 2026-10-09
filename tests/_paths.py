"""Where the tests find things, whatever directory the checkout lives in.

ROOT  the repo root holding the code under test (this checkout, or a worktree).
DATA  where the gitignored live files are (ad_history.json, the snapshot,
      vehicle_cache.db, credentials.py ...): the main checkout (found through
      git; ROOT itself when ROOT is the main checkout), or the directory in
      ADWRITER_DATA.

Importing this puts ROOT first on sys.path, so the code under test is always
the checkout the test file sits in; DATA goes last, so only the gitignored
modules (credentials.py) come from it.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    env = os.environ.get("ADWRITER_DATA")
    if env:
        return Path(env)
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        if common and (Path(common).parent / "ad_history.json").exists():
            return Path(common).parent        # the main checkout (== ROOT when ROOT is it)
    except (OSError, subprocess.SubprocessError):
        pass
    return ROOT


DATA = _data_dir()

# A test never writes to the live cost log and never asks for the main key:
# its model calls (all stubbed or network-blocked anyway) are logged to a temp
# database as purpose 'test' with the dev key class.
os.environ.setdefault("ADWRITER_COST_DB", str(Path(tempfile.mkdtemp(prefix="api_cost_test_")) / "api_cost.db"))
os.environ.setdefault("ADWRITER_COST_PURPOSE", "test")
os.environ.setdefault("ADWRITER_KEY_CLASS", "dev")

if str(ROOT) not in sys.path[:1]:
    sys.path.insert(0, str(ROOT))
if DATA != ROOT and str(DATA) not in sys.path:
    sys.path.append(str(DATA))
