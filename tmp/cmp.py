import sys, json; sys.path.insert(0,'.')
from app.core.parser import parse_log_line, infer_cause, is_continuation
cases = [
 "2026-09-02 18:22:55 ERROR voice.identity: [identity] Lookup FAILED for 916360722483 (500 Internal Server Error)",
 "2026-08-28 16:00:18.807 | INFO | api:lifespan:44 - [api] Warming models",
 '{"ts":"2026-08-28T10:00:04Z","level":"warning","component":"billing","message":"Processor slow"}',
 "INFO [api] Gateway starting - orchestrator=http://localhost:6004 timeout=45.0s",
 "  File \"tools/db.py\", line 70, in get_db",
 "\x1b[32mINFO\x1b[0m ANSI coloured line",
 "10:00:01 INFO Request received POST /api/query",
]
out=[]
for c in cases:
    p=parse_log_line(c); cz=infer_cause(p['message'])
    out.append({"level":p['level'],"ts":p['timestamp'],"msg":p['message'],
                "cause":(cz or {}).get('cause'),"cont":is_continuation(c)})
print(json.dumps(out))
