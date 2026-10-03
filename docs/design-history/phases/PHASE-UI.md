# Phase UI — the Aegis dashboard (part of C17)

**Status:** complete · **Branch:** `dev-v2` · **Tests:** 4 (`tests/test_aegis_server.py`)

---

## What and how to run

```bash
python3 -m aegis.server <logfile> [project] [port]     # default port 8600
# then open http://127.0.0.1:8600
```

Everything the platform knows, in one page, polling live:

- **The funnel** — lines → events → templates → signals → incidents
- **Run verdicts, front and centre** — ACHIEVED / HOLLOW / FAILED / DEGRADED /
  UNKNOWN cards per trace, each with its reason. The moat, visible.
- **Incidents** — expandable: members with the ranked cause marked, why it was
  ranked, precedents from memory ("precedent, not conclusion"), and an
  **Explain** button that spends exactly one governed model call when clicked
- **Flow spec panel** — every step with its presence %, CRITICAL marks and who
  set them, the path of the human-editable file, and a one-call
  "Mark purpose steps" button
- **Patterns + quiet P4 notes** — peripheral by design

Free to leave open forever: the page itself never triggers a model call;
the budget line in the header shows calls used whenever one is spent.
v1's dashboard (`app/`, port 8500) is a separate program, untouched.

## Verified

- Page JS parse-checked with JavaScriptCore (v1's blank-dashboard bugs were
  exactly this class), served live against the real log: verdicts
  {failed: 2, hollow: 6, achieved: 4}, 13 incidents, spec loaded from the
  saved human-editable file.
- `state()` never spends a model call — tested.
- An unmarked spec yields `unknown` verdicts and the page says why — tested.

## A production bug the live dashboard exposed

Incident counters restart at INC-1 each session, and the archive keyed on the
bare id — so **today's INC-1 overwrote yesterday's precedent**. Found because
the dashboard showed zero precedents for an incident that had one an hour
earlier. Archive keys now carry the opening time (`INC-1@09:00:00`): replaying
the same file replaces the same rows, a new session archives alongside the
old. Pinned as `test_a_new_sessions_incident_1_does_not_erase_last_sessions`.
After the fix, all 13 live incidents carry precedents.
