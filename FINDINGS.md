# Open findings — from real use, not from tests

Things seen while running Aegis against a real project (paideia chatbot,
667 events). Not yet fixed; parked deliberately to keep testing.

## 1. Library noise opens incidents  [HIGH]
A pydantic deprecation warning from `venv/lib/.../site-packages/` opened an
incident, ranked above a genuine failure. Lines from venv/, site-packages/,
node_modules/, .venv/ are not the user's system - a third-party warning must
never open an incident. One useless card above a real one teaches the user to
ignore the list, which costs more than the missed signal ever would.

## 2. The incident card explains the mechanism, not the consequence  [HIGH]
Card reads "NoveltyDetector @16:52:50 · RANKED CAUSE · why ranked: earliest
onset of 2 member(s)". That is how Aegis thinks, not what happened. The Now
screen was built to fix exactly this and the Incidents tab still speaks the
old language. Should read like: "A question failed - the graph returned no
node types, so every retry was guaranteed to fail."

## 3. The Brief can be worse than useless  [HIGH]
On 667 events with 9 incidents, the brief read "observability failed.
Execution completed successfully." at 0% confidence. Detectors worked; the
summariser did not. This is the screen a user actually looks at.

## 4. Simulate ignores the project brief and the code graph  [MED]
It reasons only from observed runtime dependencies. The brief and the graph
both exist now and neither reaches it.

## 5. A fresh start is not discoverable  [MED]
Three separate things persist and reattach silently:
  ~/.aegis/projects/*            learned data
  ~/.loganalyst-session.json     v1 re-attaches to the last source on boot
  ~/.loganalyst/history.db       v1 duration history
"Clear run" in the UI clears none of them. A user who wants to start over
cannot, and will conclude the tool is stuck.

## 6. Port suggestions still include stray processes  [LOW]
A Google helper on :11735 was grouped as a "scratchpad" project. Harmless
but it costs trust in a list whose whole job is to be trustworthy.
