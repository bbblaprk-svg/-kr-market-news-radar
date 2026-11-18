"""Guarded persistent-storage maintenance for the operations projection.

Only exact, explicitly retired runtime directories are removed.  The broker-owned
history database is never deleted.  History retention keeps the most recent
configurable number of KRX LS sessions so MA200 remains available with headroom.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()
_LAST_PERIODIC = 0.0


def _truthy(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _mount() -> Path:
    return Path(os.getenv("STORAGE_MOUNT_PATH", "/data")).resolve()


def _abs(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.abspath(os.path.expanduser(value)))


def _under(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _tree_usage(path: Path, max_files: int = 5000) -> dict:
    total = 0
    files = 0
    truncated = False
    try:
        if path.is_file():
            return {"bytes": int(path.stat().st_size), "files": 1, "truncated": False}
        if not path.exists():
            return {"bytes": 0, "files": 0, "truncated": False}
        for root, _dirs, names in os.walk(path):
            for name in names:
                files += 1
                if files > max_files:
                    truncated = True
                    break
                try:
                    total += Path(root, name).stat().st_size
                except OSError:
                    pass
            if truncated:
                break
        return {"bytes": int(total), "files": int(files), "truncated": truncated}
    except OSError as exc:
        return {"status": "UNAVAILABLE", "error": type(exc).__name__}


def _disk(mount: Path) -> dict:
    try:
        st = os.statvfs(mount)
        total = int(st.f_frsize * st.f_blocks)
        free = int(st.f_frsize * st.f_bavail)
        return {"total_bytes": total, "free_bytes": free, "used_bytes": max(0, total-free),
                "used_pct": round(100.0*(total-free)/total, 2) if total else None}
    except OSError as exc:
        return {"status": "UNAVAILABLE", "error": type(exc).__name__}


def _active_targets() -> list[Path]:
    # Missing env vars do NOT mean unused.  Include each module's production default
    # so cleanup always fails closed if a future deploy removes one of our overrides.
    defaults = {
        "OPERATIONS_DATA_DIR": "/data/operations_v9",
        "OPERATIONS_FALLBACK_DIR": "/tmp/operations_v9",
        "CONTEXT_LEDGER_DB_PATH": "/data/context_ledger/context_ledger.db",
        "LS_PROGRAM_DB_PATH": "/data/context_ledger/ls_program_trend.db",
        "LS_DERIVATIVES_DB_PATH": "/data/context_ledger/ls_derivatives.db",
        "CACHE_DB": "/data/alpha500_bars.sqlite3",
        "V17_ORIGINAL_HISTORY_DB": os.getenv("CACHE_DB", "/data/alpha500_bars.sqlite3"),
        "V3_HISTORY_DB": os.getenv("CACHE_DB", "/data/alpha500_bars.sqlite3"),
        "NEWS_IMPACT_DB_PATH": "/tmp/news-impact-v1.sqlite3",
        "RESULT_DIR": "/data/results",
    }
    targets=[]
    for key, default in defaults.items():
        p=_abs(os.getenv(key, default))
        if p is not None:targets.append(p)
    repair=_abs(os.getenv("KRX_DAILY_REPAIR_STATE_PATH"))
    if repair is not None:targets.append(repair)
    common=_abs(os.getenv("COMMON_GATE_RESULT_DIR"))
    if common is not None:targets.append(common)
    return targets


def cleanup_legacy_dirs() -> dict:
    """Free only exact retired directories after proving no configured target uses them."""
    mount = _mount()
    report = {"enabled": _truthy("STORAGE_CLEANUP_LEGACY", "false"), "before": _disk(mount), "removed": [], "skipped": []}
    if not report["enabled"]:
        report["after"] = report["before"]
        return report
    targets = _active_targets()
    allowed = (mount / "operations", mount / "context_ledger")
    for path in allowed:
        item = {"path": str(path), "usage": _tree_usage(path)}
        if path.is_symlink():
            item["reason"] = "SYMLINK_REFUSED"; report["skipped"].append(item); continue
        if any(_under(t, path) or t == path for t in targets):
            item["reason"] = "ACTIVE_TARGET_REFUSED"; report["skipped"].append(item); continue
        if not path.exists():
            item["reason"] = "ABSENT"; report["skipped"].append(item); continue
        try:
            # Exact allowlist only.  Never touch /data/results or alpha500_bars.* here.
            shutil.rmtree(path)
            item["removed_bytes_estimate"] = item.get("usage", {}).get("bytes", 0)
            report["removed"].append(item)
        except OSError as exc:
            item["reason"] = type(exc).__name__; report["skipped"].append(item)
    report["after"] = _disk(mount)
    return report


def _maintenance_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    # Avoid KRX/NXT operating hours.  Same safety window as daily repair.
    return now.weekday() >= 5 or now.time() < dtime(8, 0) or now.time() >= dtime(21, 0)


def prune_history(now: datetime | None = None) -> dict:
    """Bound LS/KRX history growth without touching current 200MA requirements."""
    enabled = _truthy("HISTORY_RETENTION_ENABLED", "true")
    path = _abs(os.getenv("V17_ORIGINAL_HISTORY_DB") or os.getenv("CACHE_DB") or "/data/alpha500_bars.sqlite3")
    report = {"enabled": enabled, "path": str(path) if path else None, "status": "SKIPPED", "deleted": 0}
    if not enabled or path is None or not path.exists():
        report["reason"] = "DISABLED_OR_ABSENT"; return report
    if not _maintenance_window(now):
        report["reason"] = "SESSION_PROTECTION"; return report
    try:
        keep = max(260, min(520, int(os.getenv("HISTORY_RETENTION_SESSIONS", "320"))))
    except (TypeError, ValueError):
        keep = 320
    con = None
    try:
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "bars" not in tables:
            report["reason"] = "BARS_TABLE_ABSENT"; return report
        dates = [str(r[0]) for r in con.execute(
            "SELECT DISTINCT timestamp FROM bars WHERE provider='LS' AND venue='KRX' "
            "AND timestamp GLOB '????-??-??' AND strftime('%w',timestamp) NOT IN ('0','6') ORDER BY timestamp DESC LIMIT ?", (keep,)
        ).fetchall()]
        if len(dates) < keep:
            report.update(status="READY", reason="BELOW_RETENTION_DEPTH", retained_sessions=len(dates)); return report
        cutoff = min(dates)
        before = con.total_changes
        con.execute("DELETE FROM bars WHERE provider='LS' AND venue='KRX' AND timestamp < ?", (cutoff,))
        deleted = con.total_changes - before
        con.commit()
        # Reuse freed pages.  Do not VACUUM the broker cache during runtime.
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        report.update(status="READY", reason="RETENTION_APPLIED", cutoff=cutoff,
                      retained_sessions=len(dates), deleted=int(deleted), file_bytes=int(path.stat().st_size))
        return report
    except (sqlite3.Error, OSError) as exc:
        report.update(status="PENDING", reason=type(exc).__name__, error=str(exc)[:160]); return report
    finally:
        if con is not None:
            con.close()


def bootstrap_storage() -> dict:
    """Run before Operations DB creation so full-disk recovery can succeed."""
    with _LOCK:
        return {"cleanup": cleanup_legacy_dirs(), "history_retention": prune_history()}


def periodic_storage_maintenance() -> dict:
    global _LAST_PERIODIC
    with _LOCK:
        interval = max(3600, int(os.getenv("STORAGE_MAINTENANCE_INTERVAL_SECONDS", "21600")))
        if time.monotonic() - _LAST_PERIODIC < interval:
            return {"status": "SKIPPED_INTERVAL"}
        _LAST_PERIODIC = time.monotonic()
        return {"status": "READY", "history_retention": prune_history()}


def start_periodic_storage_maintenance() -> threading.Thread:
    def loop():
        wait = max(3600, int(os.getenv("STORAGE_MAINTENANCE_INTERVAL_SECONDS", "21600")))
        while True:
            time.sleep(wait)
            try:
                periodic_storage_maintenance()
            except Exception:
                pass
    t = threading.Thread(target=loop, name="storage-maintenance-v9", daemon=True)
    t.start()
    return t
