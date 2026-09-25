"""Does a mid-flow failure become an incident with the flow named?"""
import sys, time; sys.path.insert(0,'.')
from pathlib import Path
import tempfile
from aegis.pipeline import Pipeline
from aegis.l4_understanding.flowspec import FlowMiner

tmp = Path(tempfile.mkdtemp())
log = tmp/"svc.log"; log.write_text("")
p = Pipeline("chaintest", log, store_root=tmp)

GOOD = """2026-09-25 11:00:{s:02d}.000 | INFO | api:chat:322 - [api] /chat request received request_id={t}
2026-09-25 11:00:{s1:02d}.000 | INFO | orch:ask:2866 - [orchestrator] Step 1: check_scope request_id={t}
2026-09-25 11:00:{s2:02d}.000 | INFO | orch:ask:2866 - [orchestrator] Step 4: generate request_id={t}
2026-09-25 11:00:{s3:02d}.000 | INFO | orch:ask:2931 - [orchestrator] ANSWER DELIVERED request_id={t} chars=120
"""
def feed(text):
    with log.open("a") as fh: fh.write(text)
    p.poll()

# three healthy runs to mine a spec
for i, t in enumerate(["aaa111","bbb222","ccc333"]):
    feed(GOOD.format(t=t, s=i*10, s1=i*10+1, s2=i*10+2, s3=i*10+3))

spec = FlowMiner().mine(p.trace_index.traces(), name="chaintest-call")
for st in spec.steps:
    if "ANSWER DELIVERED" in st.label:
        st.critical = True; st.critical_source = "human"
print("spec steps:", len(spec.steps), "critical:", sum(1 for s in spec.steps if s.critical))
p.spec_provider = lambda: spec

# now a run that dies mid-flow: no ANSWER DELIVERED, no error either
feed("""2026-09-25 11:01:00.000 | INFO | api:chat:322 - [api] /chat request received request_id=dead44
2026-09-25 11:01:01.000 | INFO | orch:ask:2866 - [orchestrator] Step 1: check_scope request_id=dead44
2026-09-25 11:01:02.000 | INFO | orch:ask:2866 - [orchestrator] Step 4: generate request_id=dead44
2026-09-25 11:01:03.000 | INFO | orch:ask:2900 - [orchestrator] worker returned nothing request_id=dead44
""")
# push the clock past the quiet window so the trace is judged
feed("2026-09-25 11:02:30.000 | INFO | api:chat:322 - [api] /chat request received request_id=zzz999\n")

incs = p.incidents.incidents
print("\nincidents:", len(incs))
for i in incs:
    d = i.to_dict()
    print("  id:", d["id"], "| severity:", d["severity"], "| status:", d["status"])
    print("  affected_flows:", d["affected_flows"])
    print("  deviations:", d["deviations"])
    print("  evidence[0]:", (d["evidence"] or [""])[0][:110])
