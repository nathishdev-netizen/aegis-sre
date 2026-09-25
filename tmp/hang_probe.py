import faulthandler, signal, sys, threading, time
sys.path.insert(0, '/Users/nathish/Desktop/Nathish/explore/log')
faulthandler.register(signal.SIGUSR1, all_threads=True)

import app.server as srv
t = threading.Thread(target=srv.main, daemon=True)
t.start()
time.sleep(3)

import urllib.request, json
def attach():
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.PORT}/api/attach",
        data=json.dumps({"path": "tests/fixtures/spring-boot.log"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=25)
        print("ATTACH RETURNED")
    except Exception as e:
        print("attach failed:", e)
threading.Thread(target=attach, daemon=True).start()
time.sleep(6)
print("=== stacks while hung ===", flush=True)
faulthandler.dump_traceback(all_threads=True)
