"""Fail-closed additive integration into the existing HTTP handler.
Collector functions untouched. Also installs the independent US_PRE_NXT_SYNC sidecar.
"""
from pathlib import Path
import sys

try:
    from .apply_limit_attack_live_v1 import patch_all as _limit_attack_patch_all
    from .apply_runtime_resource_guard_r2 import patch_all as _runtime_resource_guard_patch_all
    from .runtime_hotfix_v10 import compose_dashboard_file as _compose_dashboard_file, dashboard_composition_selftest as _dashboard_composition_selftest
except ImportError:
    from apply_limit_attack_live_v1 import patch_all as _limit_attack_patch_all
    from apply_runtime_resource_guard_r2 import patch_all as _runtime_resource_guard_patch_all
    from runtime_hotfix_v10 import compose_dashboard_file as _compose_dashboard_file, dashboard_composition_selftest as _dashboard_composition_selftest

MARK='OPERATIONS_PROJECTION_V1'
SYNC_MARK='US_PRE_NXT_SYNC_V1_HOOK'


def _install_sync_hook(source: str) -> str:
    """Add the sidecar hook after OPERATIONS attach, including upgrade builds.

    This does not alter KIS/LS collectors, native lanes, broker auth, or the
    Operations DB schema. The sidecar owns its own bounded SQLite ledger.
    """
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

    # Existing deployments already contain OPERATIONS_PROJECTION_V1 in the
    # embedded application source. Upgrade them in place instead of returning.
    if MARK in s:
        s=_install_sync_hook(s)
        compile(s,str(p),'exec'); p.write_text(s,encoding='utf-8')
        return

    anchor='\nclass Handler(BaseHTTPRequestHandler):\n'
    if s.count(anchor)!=1:raise RuntimeError('Handler anchor mismatch')
    integration=(
        '\n# '+MARK+'\n'
        'from operations.runtime_hotfix_v10 import apply as _operations_runtime_hotfix_v10\n'
        '_operations_runtime_hotfix_v10()\n'
        'from operations.integration import attach as _operations_attach\n'
        'OPERATIONS = _operations_attach(globals())\n'
        '# '+SYNC_MARK+'\n'
        'from operations.us_pre_nxt_sync import install as _us_pre_nxt_sync_install\n'
        'from operations import engine as _operations_engine\n'
        '_us_pre_nxt_sync_install(_operations_engine)\n'
    )
    s=s.replace(anchor,integration+anchor,1)

    anchor='    def do_GET(self):\n        path = urlparse(self.path).path\n'
    if s.count(anchor)!=1:raise RuntimeError('GET anchor mismatch')
    routes='        # OPERATIONS_PROJECTION_V1_ROUTES: same process, no collector mutation.\n        if path in {"/", "/operations"}:\n            return self._html(200, __import__(\'pathlib\').Path(\'/app/operations/dashboard.html\').read_text(encoding=\'utf-8\'))\n        if path == "/legacy":\n            return self._dashboard()\n        if path.startswith("/api/operations/"):\n            try:\n                status, payload = OPERATIONS.api(self.path)\n                return self._json(status, payload)\n            except Exception as exc:\n                return self._json(503, {"status":"PENDING", "error":type(exc).__name__})\n'

    s=s.replace(anchor,anchor+routes,1)

    anchor='    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)\n'
    if s.count(anchor)!=1:raise RuntimeError('Server anchor mismatch')
    s=s.replace(anchor,'    OPERATIONS.start()  # independent read-only scan loop\n'+anchor,1)
    compile(s,str(p),'exec'); p.write_text(s,encoding='utf-8')


if __name__=='__main__':
    # Patch only the copied /app/operations projection. If any anchor changed,
    # fail the Docker build rather than deploying a partially wired lane.
    ops_root=Path(__file__).resolve().parent
    selftest=_dashboard_composition_selftest()
    print("DASHBOARD_COMPOSITION_R4_SELFTEST", selftest)

    # Single-writer build order:
    # LIMIT_ATTACK -> HVTB/V15/Research composition -> resource guard -> HTTP wiring
    _limit_attack_patch_all(ops_root)

    dashboard_status=_compose_dashboard_file(ops_root / "dashboard.html")
    print("DASHBOARD_COMPOSITION_R4", dashboard_status)

    resource_status=_runtime_resource_guard_patch_all(Path("/app"))
    print("RUNTIME_RESOURCE_GUARD_R2", resource_status)

    patch(sys.argv[1])
