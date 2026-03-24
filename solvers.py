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
        cp = subprocess.run(
            [path, "--version"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
        )
        print(f"[solver] highs --version ok: {(cp.stdout or cp.stderr).strip()}", flush=True)
    except Exception as e:
        raise RuntimeError(f"HiGHS at {path} is not executable or incompatible: {e}")


def highs_solver():
    """
    Prefer working Python HiGHS.
    Fallback to command-line HiGHS only if needed.
    """

    # 1) Python HiGHS API via highspy
    try:
        from pulp import HiGHS
        s = HiGHS(msg=False)
        if hasattr(s, "available") and s.available():
            print("[solver] Using PuLP HiGHS Python API", flush=True)
            return s
        print("[solver] PuLP HiGHS imported but backend is not available", flush=True)
    except Exception as e:
        print(f"[solver] HiGHS Python API unavailable: {e}", flush=True)

    # 2) Command-line HiGHS
    try:
        from pulp import HiGHS_CMD

        candidate_paths = []

        env_path = os.getenv("HIGHS_BIN")
        if env_path:
            candidate_paths.append(env_path)

        auto_path = shutil.which("highs")
        if auto_path:
            candidate_paths.append(auto_path)

        candidate_paths.append("/home/site/wwwroot/bin/highs")

        seen = set()
        for path in candidate_paths:
            if not path or path in seen:
                continue
            seen.add(path)

            if os.path.exists(path):
                _ensure_executable(path)
                _assert_runs(path)

                # write logs so you can inspect actual solver output
                log_path = "/tmp/highs_pulp.log"

                solver = HiGHS_CMD(
                    path=path,
                    msg=True,
                    logPath=log_path,
                    warmStart=False,
                )

                if hasattr(solver, "available") and solver.available():
                    print(f"[solver] Using HiGHS_CMD at {path}", flush=True)
                    print(f"[solver] HiGHS log path: {log_path}", flush=True)
                    return solver

                print(f"[solver] HiGHS_CMD found at {path} but not available to PuLP", flush=True)

    except Exception as e:
        print(f"[solver] HiGHS_CMD unavailable or unhealthy: {e}", flush=True)

    raise RuntimeError(
        "No usable HiGHS solver found. Install highspy or provide a working highs binary."
    )
