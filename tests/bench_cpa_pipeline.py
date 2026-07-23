from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "lib"):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

os.environ["PANEL_STARTUP_CHECK"] = "1"

from panel import app as panel_app  # noqa: E402


ITEM_COUNT = 24
SYNTHETIC_OAUTH_DELAY_SEC = 0.08


def _reset_pipeline(cpa_dir: Path) -> None:
    os.environ[panel_app.CPA_DIR_ENV] = str(cpa_dir)
    panel_app._cpa_q = queue.Queue()
    panel_app._cpa_result_q = queue.Queue()
    panel_app._cpa_lock = threading.Lock()
    panel_app._cpa_done = set()
    panel_app._cpa_inflight = set()
    panel_app._cpa_workspace_generation = 1
    panel_app._cpa_cooldown_until = 0.0
    panel_app._cpa_state = {
        "enabled": True,
        "core_ok": True,
        "core_error": "",
        "concurrency": 1,
        "pending": 0,
        "active_workers": 0,
        "commit_pending": 0,
        "commit_active": 0,
        "ok": 0,
        "fail": 0,
        "running": False,
        "active": False,
        "last_error": "",
        "last_ok_email": "",
    }
    panel_app.CPA_DELAY = 0.02
    panel_app.AUTO_CPA = True
    panel_app._job["log_path"] = None
    panel_app._logs.clear()


def _stop_pipeline(workers, committer) -> None:
    for _ in workers:
        panel_app._cpa_q.put(None)
    panel_app._cpa_q.join()
    for worker in workers:
        worker.join(timeout=5)
    panel_app._cpa_result_q.put(None)
    panel_app._cpa_result_q.join()
    committer.join(timeout=5)
    if any(worker.is_alive() for worker in workers) or committer.is_alive():
        raise RuntimeError("pipeline threads did not stop")


def run_once(concurrency: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="cpa-pipeline-bench-") as temp_dir:
        cpa_dir = Path(temp_dir) / "cpa"
        _reset_pipeline(cpa_dir)

        def synthetic_convert(sso, email="", proxy=""):
            time.sleep(SYNTHETIC_OAUTH_DELAY_SEC)
            return {
                "email": email,
                "sso": sso,
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "auth_kind": "oauth",
            }

        panel_app.convert_one = synthetic_convert
        for index in range(ITEM_COUNT):
            queued, reason = panel_app.enqueue_cpa_convert(
                email=f"bench-{index}@example.invalid",
                sso=f"synthetic-sso-{index}",
                source="performance-probe",
                force=True,
            )
            if not queued:
                raise RuntimeError(reason)

        started = time.perf_counter()
        workers, committer = panel_app._start_cpa_pipeline_threads(concurrency)
        panel_app._cpa_q.join()
        panel_app._cpa_result_q.join()
        elapsed = time.perf_counter() - started

        index_payload = json.loads(
            panel_app.current_cpa_paths().index_path.read_text(encoding="utf-8")
        )
        result = {
            "concurrency": concurrency,
            "elapsed_sec": round(elapsed, 3),
            "throughput_per_sec": round(ITEM_COUNT / elapsed, 2),
            "files": len(list(cpa_dir.glob("xai-*.json"))),
            "index_items": len(index_payload.get("items") or {}),
            "ok": int(panel_app._cpa_state.get("ok") or 0),
            "fail": int(panel_app._cpa_state.get("fail") or 0),
            "pending": int(panel_app._cpa_state.get("pending") or 0),
            "active_workers": int(
                panel_app._cpa_state.get("active_workers") or 0
            ),
            "commit_pending": int(
                panel_app._cpa_state.get("commit_pending") or 0
            ),
            "commit_active": int(
                panel_app._cpa_state.get("commit_active") or 0
            ),
            "inflight": len(panel_app._cpa_inflight),
        }
        _stop_pipeline(workers, committer)
        return result


def main() -> int:
    results = [run_once(concurrency) for concurrency in (1, 2, 4)]
    baseline = results[0]["throughput_per_sec"]
    ratios = {
        str(result["concurrency"]): round(
            result["throughput_per_sec"] / baseline, 2
        )
        for result in results
    }
    complete = all(
        result["files"] == ITEM_COUNT
        and result["index_items"] == ITEM_COUNT
        and result["ok"] == ITEM_COUNT
        and result["fail"] == 0
        and result["pending"] == 0
        and result["active_workers"] == 0
        and result["commit_pending"] == 0
        and result["commit_active"] == 0
        and result["inflight"] == 0
        for result in results
    )
    passed = complete and ratios["2"] >= 1.5 and ratios["4"] >= 2.5
    print(
        json.dumps(
            {
                "items": ITEM_COUNT,
                "results": results,
                "speedup": ratios,
                "passed": passed,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
