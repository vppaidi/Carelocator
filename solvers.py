# solvers.py
import os
import stat
import shutil
import subprocess


def _ensure_executable(path: str):
    try:
        st = os.stat(path)
        if not (st.st_mode & stat.S_IXUSR):
            os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except FileNotFoundError:
        raise FileNotFoundError(f"HiGHS binary not found at: {path}")
    except Exception as e:
        print(f"[WARN] Could not chmod highs at {path}: {e}")


def _assert_runs(path: str):
    try:
        subprocess.run(
            [path, "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            text=True,
        )
    except Exception as e:
        raise RuntimeError(f"HiGHS at {path} is not executable or incompatible: {e}")


def highs_solver():
    """
    Prefer the Python HiGHS API for stability.
    Fall back to command-line HiGHS only if needed.
    Final fallback: CBC.
    """

    # 1. Best option: Python HiGHS API
    try:
        from pulp import HiGHS
        print("[solver] Using PuLP HiGHS Python API", flush=True)
        return HiGHS(msg=False)
    except Exception as e:
        print(f"[solver] HiGHS Python API unavailable: {e}", flush=True)

    # 2. Fallback: command-line HiGHS
    try:
        from pulp import HiGHS_CMD

        env_path = os.getenv("HIGHS_BIN")
        candidate_paths = []

        if env_path:
            candidate_paths.append(env_path)

        auto_path = shutil.which("highs")
        if auto_path:
            candidate_paths.append(auto_path)

        # legacy Azure path
        candidate_paths.append("/home/site/wwwroot/bin/highs")

        seen = set()
        for path in candidate_paths:
            if not path or path in seen:
                continue
            seen.add(path)

            if os.path.exists(path):
                _ensure_executable(path)
                _assert_runs(path)
                print(f"[solver] Using HiGHS_CMD at {path}", flush=True)
                return HiGHS_CMD(path=path, msg=False)

    except Exception as e:
        print(f"[solver] HiGHS_CMD unavailable or unhealthy: {e}", flush=True)

    # 3. Final fallback: CBC
    try:
        from pulp import PULP_CBC_CMD
        print("[solver] Falling back to CBC", flush=True)
        return PULP_CBC_CMD(msg=False)
    except Exception as e:
        raise RuntimeError(
            "No usable solver found. Install highspy, or provide a working highs binary, or ensure CBC is available."
        ) from e
