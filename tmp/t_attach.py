import sys, time, faulthandler; sys.path.insert(0,'.')
faulthandler.dump_traceback_later(15, exit=True)
from app.core.state import RuntimeState
r = RuntimeState()
t0=time.time(); r.attach_file("tests/fixtures/spring-boot.log")
print(f"attach: {time.time()-t0:.2f}s")
