import sys, tempfile, threading, time, sqlite3
sys.path.insert(0,'.')
from aegis.l3_storage.store import ProjectStore

def measure(mode):
    root = tempfile.mkdtemp()
    s = ProjectStore("p", root=root)
    if mode == "delete":
        s._conn.execute("PRAGMA journal_mode=DELETE")
    stop = threading.Event()
    def writer():
        i = 0
        while not stop.is_set():
            with s._lock:
                s._conn.execute(
                    "INSERT INTO templates (id,pattern,service,count,first_seen,last_seen,example)"
                    " VALUES (?,?,?,1,'','','') ON CONFLICT(id) DO UPDATE SET count=count+1",
                    (f"t{i%50}", "p", "svc"))
                s._conn.commit()
            i += 1
    t = threading.Thread(target=writer, daemon=True); t.start()
    time.sleep(0.3)
    worst = 0.0
    for _ in range(40):
        a = time.perf_counter()
        s.templates(limit=10)
        worst = max(worst, time.perf_counter() - a)
    stop.set(); t.join(timeout=2)
    return worst * 1000

for mode in ("delete", "wal"):
    print(f"  {mode:7} worst read while writing: {measure(mode):6.1f} ms")
