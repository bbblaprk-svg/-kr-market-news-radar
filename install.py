"""Fail-closed additive integration into the existing HTTP handler. Collector functions untouched."""
from pathlib import Path
import sys
MARK='OPERATIONS_PROJECTION_V1'

def patch(path):
    p=Path(path);s=p.read_text(encoding='utf-8')
    if MARK in s:return
    anchor='\nclass Handler(BaseHTTPRequestHandler):\n'
    if s.count(anchor)!=1:raise RuntimeError('Handler anchor mismatch')
    s=s.replace(anchor,'\n# '+MARK+'\nfrom operations.integration import attach as _operations_attach\nOPERATIONS = _operations_attach(globals())\n'+anchor,1)
    anchor='    def do_GET(self):\n        path = urlparse(self.path).path\n'
    if s.count(anchor)!=1:raise RuntimeError('GET anchor mismatch')
    routes='''        # OPERATIONS_PROJECTION_V1_ROUTES: same process, no collector mutation.
        if path in {"/", "/operations"}:
            return self._html(200, __import__('pathlib').Path('/app/operations/dashboard.html').read_text(encoding='utf-8'))
        if path == "/legacy":
            return self._dashboard()
        if path.startswith("/api/operations/"):
            try:
                status, payload = OPERATIONS.api(self.path)
                return self._json(status, payload)
            except Exception as exc:
                return self._json(503, {"status":"PENDING", "error":type(exc).__name__})
'''
    s=s.replace(anchor,anchor+routes,1)
    anchor='    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)\n'
    if s.count(anchor)!=1:raise RuntimeError('Server anchor mismatch')
    s=s.replace(anchor,'    OPERATIONS.start()  # independent read-only scan loop\n'+anchor,1)
    compile(s,str(p),'exec');p.write_text(s,encoding='utf-8')

if __name__=='__main__':patch(sys.argv[1])
