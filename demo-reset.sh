#!/bin/bash
# Re-arms the Aegis Propose-fix demo. Run this between demo runs.
#
# Applying the fix is the point of the demo, and it removes the bug - so the
# second run has nothing to find. This puts everything back to the state the
# first run started from, and it is safe to run as often as you like.
#
# What it does NOT touch: services/chatbot/tools/vector_tools.py. That holds a
# REAL fix (both transformer models pinned to CPU, because MPS crashed the
# process on any question needing both workers). Reverting it would break the
# demo's last step - the one where the chatbot finally answers.
set -e

CHATBOT=/Users/nathish/Desktop/Nathish/explore/paideia-platform-explore
AGENT=/Users/nathish/Desktop/Nathish/explore/log

echo "1/5  stopping the agent and the chatbot"
pkill -9 -f "python3 run.py" 2>/dev/null || true
for p in $(lsof -ti:3000 2>/dev/null); do kill -9 "$p" 2>/dev/null || true; done
pkill -f "uvicorn api:app" 2>/dev/null || true
sleep 3

echo "2/5  re-arming the planted bug"
cd "$CHATBOT"
# Discards Aegis's applied patch; the bug lives in commit e541814.
git checkout services/chatbot/agents/orchestrator.py
armed=$(grep -c 'parallel worker dispatch' services/chatbot/agents/orchestrator.py)
[ "$armed" = "1" ] || { echo "   FAILED: bug not armed"; exit 1; }

echo "3/5  clearing Aegis state (keeping the measured test timings)"
mv ~/.aegis/timings /tmp/aegis-timings-keep 2>/dev/null || true
rm -rf ~/.aegis ~/.loganalyst
mkdir -p ~/.aegis
mv /tmp/aegis-timings-keep ~/.aegis/timings 2>/dev/null || true

echo "4/5  emptying the log the demo reads"
# rm, not truncate: uvicorn holds the old inode open and writing to a
# truncated file back-fills it with null bytes.
rm -f "$CHATBOT/.logs/chatbot.log"

echo "5/5  starting the chatbot, then the agent"
cd "$CHATBOT/services/chatbot"
( nohup venv/bin/uvicorn api:app --host 127.0.0.1 --port 8030 --reload \
    > ../../.logs/chatbot.log 2>&1 & )
cd "$AGENT"
( nohup python3 run.py > /tmp/aegis-agent.log 2>&1 < /dev/null & )

printf "     waiting for the chatbot to warm its models"
until curl -s -m 5 -o /dev/null localhost:8030/docs 2>/dev/null; do
  printf "."; sleep 4
done
echo

echo
echo "READY - open http://localhost:3000"
echo "  1. Sources -> :8030 -> Watch"
echo "  2. Ask: Give me a complete picture of UWS and Presidency -"
echo "          fees, courses, eligibility and campus life"
echo "  3. Incidents -> the crash appears (~45s)"
echo "  4. Flow & spec -> Analyze -> $CHATBOT"
echo "  5. Explain -> Propose fix -> DRAFT"
echo "  6. Apply - run the tests for this code (~45s)"
echo "  7. Ask the same question again - it answers (~60s)"
