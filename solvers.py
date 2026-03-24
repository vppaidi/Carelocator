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
        print(f"[WARN] Could not chmod highs at {path}: {e}", flush=True)


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
    Prefer a truly available solver, not just an importable class.
    Order:
    1) PuLP HiGHS Python API, if actually available
    2) HiGHS_CMD, if binary works
    3) CBC fallback
    """

    # 1. Try PuLP HiGHS Python API only if backend is actually available
    try:
        from pulp import HiGHS
        s = HiGHS(msg=False)
        if hasattr(s, "available") and s.available():
            print("[solver] Using PuLP HiGHS Python API", flush=True)
            return s
        else:
            print("[solver] PuLP HiGHS imported but backend is not available", flush=True)
    except Exception as e:
        print(f"[solver] HiGHS Python API unavailable: {e}", flush=True)

    # 2. Try command-line HiGHS
    try:
        from pulp import HiGHS_CMD

        candidate_paths = []

        env_path = os.getenv("HIGHS_BIN")
        if env_path:
            candidate_paths.append(env_path)

        auto_path = shutil.which("highs")
        if auto_path:
            candidate_paths.append(auto_path)

        # optional known path
        candidate_paths.append("/home/site/wwwroot/bin/highs")

        seen = set()
        for path in candidate_paths:
            if not path or path in seen:
                continue
            seen.add(path)

            if os.path.exists(path):
                _ensure_executable(path)
                _assert_runs(path)
                solver = HiGHS_CMD(path=path, msg=False)
                if hasattr(solver, "available") and solver.available():
                    print(f"[solver] Using HiGHS_CMD at {path}", flush=True)
                    return solver
                else:
                    print(f"[solver] HiGHS_CMD found at {path} but not available to PuLP", flush=True)

    except Exception as e:
        print(f"[solver] HiGHS_CMD unavailable or unhealthy: {e}", flush=True)

    # 3. Fallback to CBC
    try:
        from pulp import PULP_CBC_CMD
        solver = PULP_CBC_CMD(msg=False)
        if hasattr(solver, "available") and solver.available():
            print("[solver] Falling back to CBC", flush=True)
            return solver
        raise RuntimeError("CBC solver is not available")
    except Exception as e:
        raise RuntimeError(
            "No usable solver found. Install highspy, provide a working highs binary, or ensure CBC is available."
        ) from e
