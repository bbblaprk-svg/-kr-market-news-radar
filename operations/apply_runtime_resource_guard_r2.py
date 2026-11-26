"""Build-time resource guard R2 for auxiliary common-gate sidecar.

Changes only the read-only common-gate sidecar. It does not touch KIS/LS auth,
collector routes, order APIs, lane thresholds, or the main ALPHA500 process.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "COMMON_GATE_SIDECAR_V1_1_ADAPTIVE_CPU"


def patch_common_gate_runner(path: Path) -> str:
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    if MARKER in s:
        compile(s, str(p), "exec")
        return "READY_ALREADY_PATCHED"

    replacements = [
        (
            'SIDECAR_VERSION="COMMON_GATE_SIDECAR_V1_0"',
            'SIDECAR_VERSION="COMMON_GATE_SIDECAR_V1_1_ADAPTIVE_CPU"',
        ),
        (
            'POLL=max(5,int(os.getenv("COMMON_GATE_POLL_SECONDS","15")))\n'
            'STALE_SECONDS=max(60,int(os.getenv("COMMON_GATE_STALE_SECONDS","900")))\n'
            'PROFILES=',
            'POLL=max(5,int(os.getenv("COMMON_GATE_POLL_SECONDS","15")))\n'
            'FULL_REEVAL_SECONDS=max(POLL,int(os.getenv("COMMON_GATE_FULL_REEVAL_SECONDS","60")))\n'
            'ACTIVE_REEVAL_SECONDS=max(POLL,int(os.getenv("COMMON_GATE_ACTIVE_REEVAL_SECONDS","15")))\n'
            'IDLE_LOG_SECONDS=max(FULL_REEVAL_SECONDS,int(os.getenv("COMMON_GATE_IDLE_LOG_SECONDS","300")))\n'
            'SKIP_STALE_SOURCE=os.getenv("COMMON_GATE_SKIP_STALE_SOURCE","true").strip().lower() in {"1","true","yes","y","on"}\n'
            'STALE_SECONDS=max(60,int(os.getenv("COMMON_GATE_STALE_SECONDS","900")))\n'
            'PROFILES=',
        ),
        (
            '    if not isinstance(summary,dict):\n'
            '        return {"sidecar_version":SIDECAR_VERSION,"framework_version":FRAMEWORK_VERSION,"venue":venue,"status":"NO_SOURCE","source":str(src),"generated_at_kst":now_iso(),"profiles":{}}\n'
            '    rows=build_auto_rows(summary,manual); add_manual_rows(rows,manual,venue)\n',
            '    if not isinstance(summary,dict):\n'
            '        return {"sidecar_version":SIDECAR_VERSION,"framework_version":FRAMEWORK_VERSION,"venue":venue,"status":"NO_SOURCE","source":str(src),"generated_at_kst":now_iso(),"profiles":{}}\n'
            '    # Stale input cannot become actionable; skip hundreds of doomed evaluations.\n'
            '    if SKIP_STALE_SOURCE and snapshot_stale(summary):\n'
            '        return {\n'
            '            "sidecar_version":SIDECAR_VERSION,"framework_version":FRAMEWORK_VERSION,"venue":venue,\n'
            '            "status":"STALE_SOURCE_SKIPPED","source":str(src),"generated_at_kst":now_iso(),\n'
            '            "source_timestamp_kst":summary.get("timestamp_kst"),"candidate_rows":0,"profiles":{},\n'
            '            "separation":"READ_ONLY_SIDECAR_NO_MAIN_ENGINE_IMPORT_NO_DECISION_MIXING",\n'
            '        }\n'
            '    rows=build_auto_rows(summary,manual); add_manual_rows(rows,manual,venue)\n',
        ),
        (
            'def main():\n'
            '    once=os.getenv("COMMON_GATE_RUN_ONCE","false").strip().lower() in {"1","true","yes","y","on"}\n'
            '    while True:\n'
            '        try:\n'
            '            z=run_once()\n'
            '            brief={v:{p:d.get("counts") for p,d in (z["venues"][v].get("profiles") or {}).items()} for v in ("KRX","NXT")}\n'
            '            print(json.dumps({"event":"common_gate_sidecar","at":now_iso(),"brief":brief},ensure_ascii=False),flush=True)\n'
            '        except Exception as e:\n'
            '            print(json.dumps({"event":"common_gate_sidecar_error","at":now_iso(),"error":f"{type(e).__name__}: {e}","trace":traceback.format_exc(limit=3)},ensure_ascii=False),flush=True)\n'
            '        if once: break\n'
            '        time.sleep(POLL)\n',
            'def _active_count(z):\n'
            '    total=0\n'
            '    try:\n'
            '        for venue in ("KRX","NXT"):\n'
            '            for d in (z.get("venues",{}).get(venue,{}).get("profiles") or {}).values():\n'
            '                c=d.get("counts") or {}\n'
            '                total += int(c.get("ENTER",0) or 0)+int(c.get("ARMED",0) or 0)+int(c.get("FORMING",0) or 0)\n'
            '    except Exception:\n'
            '        return 0\n'
            '    return total\n\n'
            'def main():\n'
            '    once=os.getenv("COMMON_GATE_RUN_ONCE","false").strip().lower() in {"1","true","yes","y","on"}\n'
            '    last_eval=0.0; last_log=0.0; last_active=0; last_out=None\n'
            '    while True:\n'
            '        now=time.monotonic()\n'
            '        cadence=ACTIVE_REEVAL_SECONDS if last_active>0 else FULL_REEVAL_SECONDS\n'
            '        due=(last_out is None) or (now-last_eval>=cadence)\n'
            '        try:\n'
            '            if due:\n'
            '                z=run_once(); last_out=z; last_eval=now; last_active=_active_count(z)\n'
            '                brief={v:{p:d.get("counts") for p,d in (z["venues"][v].get("profiles") or {}).items()} for v in ("KRX","NXT")}\n'
            '                print(json.dumps({"event":"common_gate_sidecar","at":now_iso(),"mode":"ACTIVE_FAST" if last_active else "FULL_IDLE","active":last_active,"cadence_seconds":ACTIVE_REEVAL_SECONDS if last_active else FULL_REEVAL_SECONDS,"brief":brief},ensure_ascii=False),flush=True)\n'
            '                last_log=now\n'
            '            elif now-last_log>=IDLE_LOG_SECONDS:\n'
            '                print(json.dumps({"event":"common_gate_sidecar_idle","at":now_iso(),"active":last_active,"next_eval_in_seconds":max(0,int(cadence-(now-last_eval)))},ensure_ascii=False),flush=True)\n'
            '                last_log=now\n'
            '        except Exception as e:\n'
            '            print(json.dumps({"event":"common_gate_sidecar_error","at":now_iso(),"error":f"{type(e).__name__}: {e}","trace":traceback.format_exc(limit=3)},ensure_ascii=False),flush=True)\n'
            '        if once: break\n'
            '        time.sleep(POLL)\n',
        ),
    ]

    for old, new in replacements:
        if new in s:
            continue
        n = s.count(old)
        if n != 1:
            raise RuntimeError(f"COMMON_GATE_R2_ANCHOR_MISMATCH count={n}: {old[:80]}")
        s = s.replace(old, new, 1)

    if MARKER not in s or "STALE_SOURCE_SKIPPED" not in s or "FULL_REEVAL_SECONDS" not in s:
        raise RuntimeError("COMMON_GATE_R2_VALIDATION_FAILED")
    compile(s, str(p), "exec")
    p.write_text(s, encoding="utf-8")
    return "READY_PATCHED_R2"


def patch_all(app_root: Path = Path("/app")) -> str:
    path = Path(app_root) / "공통_게이트우선_독립구동_V1_0.py"
    if not path.exists():
        raise RuntimeError(f"COMMON_GATE_RUNNER_NOT_FOUND:{path}")
    return patch_common_gate_runner(path)


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app")
    print(patch_all(root))
