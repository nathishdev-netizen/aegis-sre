import sys, threading, time, json, faulthandler; sys.path.insert(0,'.')
import urllib.request
import app.server as srv
threading.Thread(target=srv.main, daemon=True).start()
time.sleep(3)
def attach():
    req = urllib.request.Request(f"http://127.0.0.1:{srv.PORT}/api/attach",
        data=json.dumps({"path":"tests/fixtures/spring-boot.log"}).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20); print("RETURNED")
    except Exception as e: print("failed:", e)
threading.Thread(target=attach, daemon=True).start()
time.sleep(7)
faulthandler.dump_traceback(all_threads=True)
