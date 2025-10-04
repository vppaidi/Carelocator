# solvers.py
import os
import stat
import subprocess
from pulp import HiGHS_CMD

def _ensure_executable(path: str):
    try:
        st = os.stat(path)
        if not (st.st_mode & stat.S_IXUSR):
            os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except FileNotFoundError:
        raise FileNotFoundError(f"HiGHS binary not found at: {path}")
    except Exception as e:
        # Not fatal yet; let version check raise if needed
        print(f"[WARN] Could not chmod highs at {path}: {e}")

def _assert_runs(path: str):
    try:
        # quick sanity: print version (won't be noisy in logs)
        subprocess.run([path, "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    except Exception as e:
        raise RuntimeError(f"HiGHS at {path} is not executable or incompatible: {e}")

def highs_solver():
    path = os.getenv("HIGHS_BIN", "/home/site/wwwroot/bin/highs")
    _ensure_executable(path)
    _assert_runs(path)   # early, clear error if wrong binary
    return HiGHS_CMD(path=path, msg=False)
