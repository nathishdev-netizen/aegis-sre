import sys, tempfile, threading, time
sys.path.insert(0,'.')
from aegis.l3_storage.store import ProjectStore

def measure(mode, batch=400):
    root = tempfile.mkdtemp()
    s = ProjectStore("p", root=root)
    if mode == "delete":
        s._conn.execute("PRAGMA journal_mode=DELETE")
    stop = threading.Event()
    def writer():
        i = 0
        while not stop.is_set():
            # a realistic ingest burst: many rows, one commit - what a
            # backfill or a provider page actually does
            with s._lock:
                for k in range(batch):
                    s._conn.execute(
                        "INSERT INTO templates (id,pattern,service,count,first_seen,last_seen,example)"
                        " VALUES (?,?,?,1,'','','') ON CONFLICT(id) DO UPDATE SET count=count+1",
                        (f"t{(i*batch+k)%5000}", "pattern here", "svc"))
                s._conn.commit()
            i += 1
    t = threading.Thread(target=writer, daemon=True); t.start()
    time.sleep(0.4)
    worst = 0.0; total = 0.0; n = 0
    for _ in range(60):
        a = time.perf_counter()
        s.templates(limit=10)
        d = time.perf_counter() - a
        worst = max(worst, d); total += d; n += 1
        time.sleep(0.005)
    stop.set(); t.join(timeout=3)
    return worst*1000, (total/n)*1000

for mode in ("delete", "wal"):
    w, avg = measure(mode)
    print(f"  {mode:7} worst {w:7.1f} ms   mean {avg:6.2f} ms")
