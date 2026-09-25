import sys, threading, time, json, faulthandler; sys.path.insert(0,'.')
import app.server as srv
import urllib.request
threading.Thread(target=srv.main, daemon=True).start()
time.sleep(3)
def attach():
    req = urllib.request.Request(f"http://127.0.0.1:{srv.PORT}/api/attach",
        data=json.dumps({"path":"tests/fixtures/spring-boot.log"}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    try: urllib.request.urlopen(req, timeout=20)
    except Exception: pass
threading.Thread(target=attach, daemon=True).start()
time.sleep(6)
# who owns the runtime lock?
lk = srv.runtime._lock
print("LOCK owner thread id:", getattr(lk, "_owner", None), "count:", getattr(lk, "_count", None), flush=True)
for t in threading.enumerate():
    print(f"  thread {t.ident} {t.name}", flush=True)
faulthandler.dump_traceback(all_threads=True)
