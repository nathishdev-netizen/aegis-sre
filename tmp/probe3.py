import sys, threading, time, json; sys.path.insert(0,'.')
sys.argv=['x']
import app.server as srv
from app.core.state import RuntimeState

original = RuntimeState.attach_file
def timed(self, p):
    t0=time.time()
    try:
        return original(self, p)
    finally:
        print(f"  [attach_file] {time.time()-t0:.2f}s", flush=True)
RuntimeState.attach_file = timed

original_bc = RuntimeState.broadcast
def timed_bc(self):
    t0=time.time()
    try:
        return original_bc(self)
    finally:
        d=time.time()-t0
        if d > 0.5: print(f"  [broadcast] SLOW {d:.2f}s", flush=True)
RuntimeState.broadcast = timed_bc

threading.Thread(target=srv.main, daemon=True).start()
time.sleep(3)
import urllib.request
req = urllib.request.Request(f"http://127.0.0.1:{srv.PORT}/api/attach",
    data=json.dumps({"path":"tests/fixtures/spring-boot.log"}).encode(),
    headers={"Content-Type":"application/json"}, method="POST")
t0=time.time()
try:
    urllib.request.urlopen(req, timeout=25); print(f"HTTP attach returned in {time.time()-t0:.2f}s")
except Exception as e: print(f"HTTP attach failed after {time.time()-t0:.1f}s: {e}")
