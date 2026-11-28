"""Fail-closed additive integration into the existing HTTP handler.
Collector functions untouched. Also installs the independent US_PRE_NXT_SYNC sidecar.
"""
from pathlib import Path
import sys

try:
    from .apply_limit_attack_live_v1 import patch_all as _limit_attack_patch_all
    from .apply_runtime_resource_guard_r2 import patch_all as _runtime_resource_guard_patch_all
    from .apply_candidate_visibility_repair_r5 import patch_all as _candidate_visibility_patch_all
    from .runtime_hotfix_v10 import compose_dashboard_file as _compose_dashboard_file, dashboard_composition_selftest as _dashboard_composition_selftest
    from .nxt_lane_repair_r8_3_1 import patch_dashboard_file as _nxt_r8_patch_dashboard, selftest as _nxt_r8_selftest
except ImportError:
    from apply_limit_attack_live_v1 import patch_all as _limit_attack_patch_all
    from apply_runtime_resource_guard_r2 import patch_all as _runtime_resource_guard_patch_all
    from apply_candidate_visibility_repair_r5 import patch_all as _candidate_visibility_patch_all
    from runtime_hotfix_v10 import compose_dashboard_file as _compose_dashboard_file, dashboard_composition_selftest as _dashboard_composition_selftest
    from nxt_lane_repair_r8_3_1 import patch_dashboard_file as _nxt_r8_patch_dashboard, selftest as _nxt_r8_selftest

MARK='OPERATIONS_PROJECTION_V1'
SYNC_MARK='US_PRE_NXT_SYNC_V1_HOOK'
NXT_R8_MARK='NXT_LANE_REPAIR_R8_HOOK'


def _install_nxt_r8_hook(source: str) -> str:
    """Install/upgrade NXT R8.3.1 after runtime hotfix, before Operations attach."""
    legacy_imports=(
        'from operations.nxt_lane_repair_r8 import apply as _nxt_lane_repair_r8_apply\n',
        'from operations.nxt_lane_repair_r8_1 import apply as _nxt_lane_repair_r8_apply\n',
        'from operations.nxt_lane_repair_r8_2 import apply as _nxt_lane_repair_r8_apply\n',
        'from operations.nxt_lane_repair_r8_3 import apply as _nxt_lane_repair_r8_apply\n',
        'from operations.nxt_lane_repair_r8_3_1 import apply as _nxt_lane_repair_r8_apply\n',
    )
    new_import='from operations.nxt_lane_repair_r8_3_1 import apply as _nxt_lane_repair_r8_apply\n'

    if NXT_R8_MARK in source:
        hits=[x for x in legacy_imports if x in source]
        if len(hits) > 1:
            raise RuntimeError('NXT_R8_3_1 duplicate legacy imports')
        if hits:
            source=source.replace(hits[0],new_import,1)
        if source.count(new_import) != 1:
            raise RuntimeError('NXT_R8_3_1 existing hook import mismatch')
        return source

    anchor='from operations.integration import attach as _operations_attach\n'
    if source.count(anchor) != 1:
        raise RuntimeError('NXT_R8 attach anchor mismatch')
    hook=(
        '# '+NXT_R8_MARK+'\n'
        'from operations import engine as _operations_engine\n'
        +new_import+
        '_nxt_lane_repair_r8_apply(_operations_engine)\n'
        +anchor
    )
    return source.replace(anchor, hook, 1)


def _install_sync_hook(source: str) -> str:
    """Add R8.3.1 and US_PRE_NXT_SYNC after OPERATIONS integration."""
    source=_install_nxt_r8_hook(source)
    if SYNC_MARK in source:
        return source
    anchor='OPERATIONS = _operations_attach(globals())\n'
    if source.count(anchor) != 1:
        raise RuntimeError('US_PRE_NXT_SYNC attach anchor mismatch')
    hook=(
        anchor+
        '# '+SYNC_MARK+'\n'
        'from operations.us_pre_nxt_sync import install as _us_pre_nxt_sync_install\n'
        'from operations import engine as _operations_engine\n'
        '_us_pre_nxt_sync_install(_operations_engine)\n'
    )
    return source.replace(anchor, hook, 1)


def patch(path):
    p=Path(path); s=p.read_text(encoding='utf-8')

    if MARK in s:
        s=_install_sync_hook(s)
        compile(s,str(p),'exec')
        p.write_text(s,encoding='utf-8')
        return

    anchor='\nclass Handler(BaseHTTPRequestHandler):\n'
    if s.count(anchor)!=1:
        raise RuntimeError('Handler anchor mismatch')
    integration=(
        '\n# '+MARK+'\n'
        'from operations.runtime_hotfix_v10 import apply as _operations_runtime_hotfix_v10\n'
        '_operations_runtime_hotfix_v10()\n'
        '# '+NXT_R8_MARK+'\n'
        'from operations import engine as _operations_engine\n'
        'from operations.nxt_lane_repair_r8_3_1 import apply as _nxt_lane_repair_r8_apply\n'
        '_nxt_lane_repair_r8_apply(_operations_engine)\n'
        'from operations.integration import attach as _operations_attach\n'
        'OPERATIONS = _operations_attach(globals())\n'
        '# '+SYNC_MARK+'\n'
        'from operations.us_pre_nxt_sync import install as _us_pre_nxt_sync_install\n'
        '_us_pre_nxt_sync_install(_operations_engine)\n'
    )
    s=s.replace(anchor,integration+anchor,1)

    anchor='    def do_GET(self):\n        path = urlparse(self.path).path\n'
    if s.count(anchor)!=1:
        raise RuntimeError('GET anchor mismatch')
    routes='        # OPERATIONS_PROJECTION_V1_ROUTES: same process, no collector mutation.\n        if path in {"/", "/operations"}:\n            return self._html(200, __import__(\'pathlib\').Path(\'/app/operations/dashboard.html\').read_text(encoding=\'utf-8\'))\n        if path == "/legacy":\n            return self._dashboard()\n        if path.startswith("/api/operations/"):\n            try:\n                status, payload = OPERATIONS.api(self.path)\n                return self._json(status, payload)\n            except Exception as exc:\n                return self._json(503, {"status":"PENDING", "error":type(exc).__name__})\n'
    s=s.replace(anchor,anchor+routes,1)

    anchor='    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)\n'
    if s.count(anchor)!=1:
        raise RuntimeError('Server anchor mismatch')
    s=s.replace(anchor,'    OPERATIONS.start()  # independent read-only scan loop\n'+anchor,1)

    compile(s,str(p),'exec')
    p.write_text(s,encoding='utf-8')


if __name__=='__main__':
    ops_root=Path(__file__).resolve().parent
    selftest=_dashboard_composition_selftest()
    print("DASHBOARD_COMPOSITION_R4_SELFTEST", selftest)

    visibility_status=_candidate_visibility_patch_all(ops_root)
    print("CANDIDATE_VISIBILITY_R5", visibility_status)

    _limit_attack_patch_all(ops_root)

    dashboard_status=_compose_dashboard_file(ops_root / "dashboard.html")
    print("DASHBOARD_COMPOSITION_R4", dashboard_status)

    nxt_ui_status=_nxt_r8_patch_dashboard(ops_root / "dashboard.html")
    print("NXT_LANE_REPAIR_R8_3_1_UI", nxt_ui_status)
    print("NXT_LANE_REPAIR_R8_3_1_SELFTEST", _nxt_r8_selftest())

    resource_status=_runtime_resource_guard_patch_all(Path("/app"))
    print("RUNTIME_RESOURCE_GUARD_R2", resource_status)

    patch(sys.argv[1])
