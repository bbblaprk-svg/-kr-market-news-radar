#!/usr/bin/env python3
"""Candidate visibility/proof repair R5.

Fixes one contradictory gate in operations/engine.py:
whole-market authority completeness is metadata for coverage, not a veto on a
stock that has fresh producer evidence and is present in the current authority
quote map. Missing/unverified symbols remain PENDING and never become entries.
"""
from __future__ import annotations
from pathlib import Path

MARK = "CANDIDATE_VISIBILITY_REPAIR_R5"


def _proof_scope(ready, qmap, applicable=True):
    if not applicable:
        return set()
    return set(ready or ()) & set(qmap or {})


def selftest() -> str:
    q={"000120":{},"003380":{}}
    assert _proof_scope({"000120","999999"},q,True)=={"000120"}
    assert _proof_scope({"000120"},q,False)==set()
    assert _proof_scope(set(),q,True)==set()
    return "PASS"


def patch_engine(path: Path) -> str:
    p=Path(path)
    s=p.read_text(encoding="utf-8")
    if MARK in s:
        compile(s,str(p),"exec")
        return "READY_ALREADY_PATCHED"
    old="                        lane_complete=ready if complete and applicable else set()\n"
    new=(
        "                        # "+MARK+": whole-market completeness must not erase fresh per-symbol proof.\n"
        "                        # Only producer-verified codes that are also present in the current authority qmap are confirmed.\n"
        "                        lane_complete=(set(ready) & set(qmap)) if applicable else set()\n"
    )
    n=s.count(old)
    if n!=1:
        raise RuntimeError(f"CANDIDATE_VISIBILITY_R5_ENGINE_ANCHOR_COUNT={n}")
    s=s.replace(old,new,1)
    if MARK not in s:
        raise RuntimeError("CANDIDATE_VISIBILITY_R5_MARKER_MISSING")
    compile(s,str(p),"exec")
    p.write_text(s,encoding="utf-8")
    return "READY_PATCHED_R5"


def patch_all(root: Path|str) -> str:
    if selftest()!="PASS":
        raise RuntimeError("CANDIDATE_VISIBILITY_R5_SELFTEST_FAILED")
    root=Path(root)
    return patch_engine(root/"engine.py")


if __name__=="__main__":
    import sys
    root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent
    print("CANDIDATE_VISIBILITY_R5_SELFTEST",selftest())
    print("CANDIDATE_VISIBILITY_R5",patch_all(root))
