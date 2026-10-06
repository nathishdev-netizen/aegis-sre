#!/bin/bash
# The recording spine: plays Aegis's whole story as eight paced scenes.
#
#   ./demo.sh                 every scene, pausing between each
#   ./demo.sh 3 4 5           only those scenes
#   ./demo.sh --list          what the scenes are
#   ./demo.sh --auto          no pauses (for a dry run or a CI smoke check)
#   ./demo.sh --check         verify the environment, run nothing
#
# Each scene is one of the aegis.demo scripts, which already narrate
# themselves. What this adds is order, a spoken-aloud cue before each one,
# and a pause so a take can be narrated over it. It reads logs and writes
# only under ~/.aegis - the same promise the product makes.
#
# Scene 6 and 7 need a repo to analyse and a planted bug; they say so and
# skip rather than fail, so a recording never dies halfway.
set -uo pipefail

AGENT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$AGENT"

# The benchmark log: 12 calls, 2 failed, 6 hollow, 4 achieved. Overridable,
# because a product has no business hardcoding one machine's paths.
REF="${AEGIS_DEMO_LOG:-/Users/nathish/Desktop/Nathish/tt/Demos/voice-gateway/.logs/voice-gateway.log}"
FIXTURE="${AEGIS_DEMO_FIXTURE:-$AGENT/tests/fixtures/json-lines.log}"
REPO="${AEGIS_DEMO_REPO:-/Users/nathish/Desktop/Nathish/explore/paideia-platform-explore}"

AUTO=0; CHECK=0; WANT=()
for a in "$@"; do
  case "$a" in
    --auto)  AUTO=1 ;;
    --check) CHECK=1 ;;
    --list)  LIST=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) WANT+=("$a") ;;
  esac
done

if [ -t 1 ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; CY=$'\033[36m'; GN=$'\033[32m'; YL=$'\033[33m'; RD=$'\033[31m'; Z=$'\033[0m'
else
  B=""; DIM=""; CY=""; GN=""; YL=""; RD=""; Z=""
fi

TITLES=(
  "The hollow run - a call that completed and achieved nothing"
  "Learning the vocabulary - redaction, then the volume funnel"
  "The join key - 11% of lines carry one, 89% are inferred"
  "Detection by arithmetic - no model, no thresholds to tune"
  "The moat - every run judged against a spec mined from itself"
  "Grounded explanation - citations checked, fabrications dropped"
  "Code intelligence - from a log line to the file and line"
  "Memory - the second incident starts from the first's outcome"
)

say() { printf '\n%s%s%s\n' "$CY" "$1" "$Z"; }
note() { printf '%s%s%s\n' "$DIM" "$1" "$Z"; }
ok()   { printf '%s  ok %s%s\n' "$GN" "$1" "$Z"; }
warn() { printf '%s  -- %s%s\n' "$YL" "$1" "$Z"; }
bad()  { printf '%s  !! %s%s\n' "$RD" "$1" "$Z"; }

banner() {
  printf '\n%s' "$B"
  printf '=%.0s' $(seq 74); printf '\n'
  printf ' SCENE %s   %s\n' "$1" "$2"
  printf '=%.0s' $(seq 74); printf '%s\n' "$Z"
}

# The cue is what you say out loud while the screen is still still.
cue() { printf '%s  say: %s%s\n\n' "$YL" "$1" "$Z"; }

pause() {
  [ "$AUTO" = "1" ] && { printf '\n'; return; }
  printf '\n%s  [enter] next scene  ·  [s] skip  ·  [q] quit%s ' "$DIM" "$Z"
  read -r -n1 k; printf '\n'
  case "$k" in q|Q) echo "stopped."; exit 0 ;; esac
}

if [ "${LIST:-0}" = "1" ]; then
  echo "${B}Aegis demo scenes${Z}"
  for i in "${!TITLES[@]}"; do printf '  %s  %s\n' "$((i+1))" "${TITLES[$i]}"; done
  exit 0
fi

# ---------------------------------------------------------------- preflight
preflight() {
  say "PREFLIGHT"
  local fatal=0

  if [ -f "$REF" ]; then
    ok "benchmark log  $(wc -l < "$REF" | tr -d ' ') lines"
  else
    bad "benchmark log missing: $REF"
    note "     set AEGIS_DEMO_LOG, or generate one:"
    note "     python3 demo/log_generator.py --scenario mixed --count 120 \\"
    note "       --file /tmp/demo.log --seed 7"
    fatal=1
  fi

  [ -f "$FIXTURE" ] && ok "fixture log    $(basename "$FIXTURE")" \
                    || warn "fixture log missing - scene 2 falls back to the benchmark log"

  if grep -qE '^(GROQ|OPENAI)_API_KEY=.+' .env 2>/dev/null; then
    ok "model key      found in .env (Groq free tier is the default)"
  else
    warn "no model key - scenes 6 and 8 will skip their model call"
  fi

  # The saved spec is what makes scene 5 reproduce the benchmark: the
  # purpose step is a HUMAN's call, and re-mining would overwrite it.
  local spec="$HOME/.aegis/projects/$(basename "$REF" .log)/flows/$(basename "$REF" .log)-call.json"
  if [ -f "$spec" ] && grep -q '"critical": true' "$spec" 2>/dev/null; then
    ok "flow spec      purpose step already marked"
  else
    warn "flow spec has no critical step yet - scene 5 will mine and ask the model"
    note "     after it runs, mark the real purpose step and rerun with --use-saved-spec"
  fi

  if [ -d "$REPO/.git" ]; then
    ok "target repo    $(basename "$REPO")"
    if grep -rqs 'parallel worker dispatch' "$REPO/services/chatbot/agents/orchestrator.py" 2>/dev/null; then
      ok "planted bug    armed"
    else
      warn "planted bug not armed - run ./demo-reset.sh for the live fix demo"
    fi
  else
    warn "no target repo at $REPO - scene 7 explains and skips"
  fi

  printf '\n'
  [ "$fatal" = "1" ] && { bad "cannot run without a log file."; exit 1; }
  [ "$CHECK" = "1" ] && { ok "preflight only - nothing was run."; exit 0; }
}

wanted() {
  [ ${#WANT[@]} -eq 0 ] && return 0
  for w in "${WANT[@]}"; do [ "$w" = "$1" ] && return 0; done
  return 1
}

# ------------------------------------------------------------------- scenes
scene1() {
  banner 1 "${TITLES[0]}"
  cue "Every dashboard said these calls were fine. Watch what they actually did."
  python3 -m aegis.demo.conformance "$REF" --use-saved-spec 2>&1 \
    | sed -n '/EVERY TRACE, JUDGED/,/HOW TO CHECK/p' | sed '$d' | head -34
  note "  Four calls did the work. Six exited cleanly having done none of it."
  note "  No error fired, no threshold tripped, every dashboard stayed green."
}

scene2() {
  banner 2 "${TITLES[1]}"
  cue "Before anything is stored, the sensitive values are gone - and millions of lines become hundreds of shapes."
  local src="$FIXTURE"; [ -f "$src" ] || src="$REF"
  python3 -m aegis.demo.phase0 "$src"
}

scene3() {
  banner 3 "${TITLES[2]}"
  cue "One request's story is scattered across hundreds of lines. Only one in nine names its own id."
  python3 -m aegis.demo.phase1 "$REF" 2>&1 | sed -n '/HOW TO CHECK/q;p'
  note "  [x] extracted is evidence. [i] inferred is an assumption, and it is"
  note "  recorded as one - when two calls overlap, inference stops."
}

scene4() {
  banner 4 "${TITLES[3]}"
  cue "No model ran here. Every signal is counting, ratios and medians - which is why it is affordable."
  python3 -m aegis.demo.phase2 "$REF" demo-spine 2>&1 | head -52
}

scene5() {
  banner 5 "${TITLES[4]}"
  cue "The spec is mined from the project's own traces. But which step is the PURPOSE is a human's call - frequency cannot find it."
  python3 -m aegis.demo.conformance "$REF" --use-saved-spec
  note "  A greeting appears in every call, so mining marks it required."
  note "  A call that only greeted achieved nothing. That is why 'critical'"
  note "  is never auto-derived from frequency."
}

scene6() {
  banner 6 "${TITLES[5]}"
  cue "Now a model speaks - once, governed, and every line it cites is checked against the evidence it was given."
  python3 -m aegis.demo.explain "$REF" --n 1
  note "  'grounded' means every citation was found in the evidence it was given."
  note "  Any it invented are dropped and counted; if none survive, the label"
  note "  reads UNVERIFIED and confidence is forced to low - an opinion, not a"
  note "  finding. Nothing here is taken on the model's word."
}

scene7() {
  banner 7 "${TITLES[6]}"
  cue "A log line has no pointer to the code that wrote it. This is the bridge - and once it exists, it also becomes the fence the patch may not cross."
  if [ -d "$REPO/.git" ]; then
    python3 -m aegis.demo.remediate "$REPO"
    note ""
    note "  If that said ADVISE / 'no evidence line maps to source', that IS the"
    note "  scene: nothing mapped, so it refuses to guess and names what is"
    note "  missing instead. The full reproduce-and-patch take needs the repo"
    note "  analysed first, which the UI does - see ./demo-reset.sh."
  else
    warn "no repo at $REPO"
    note "  This scene needs a real repo with a real bug. Point at one with:"
    note "    AEGIS_DEMO_REPO=/path/to/repo ./demo.sh 7"
    note "  Or run ./demo-reset.sh and do it live in the UI, which is the"
    note "  stronger take: the chatbot crashes, Aegis proposes, you apply,"
    note "  and the same question then answers."
  fi
  note "  The reproducer must FAIL before the patch. If it passes, the"
  note "  diagnosis was wrong and the agent stops rather than guessing."
}

scene8() {
  banner 8 "${TITLES[7]}"
  cue "The second time this happens, the investigation does not start from a blank page."
  python3 -m aegis.demo.memory "$REF"
  note "  The precedent carries its score and the words 'precedent, not"
  note "  conclusion'. A remembered WRONG diagnosis is kept just as"
  note "  carefully as a right one."
}

# --------------------------------------------------------------------- main
preflight

printf '%s' "$B"
printf '=%.0s' $(seq 74); printf '\n'
echo " AEGIS - what it learns, what it finds, and what it hands you"
printf '=%.0s' $(seq 74); printf '%s\n' "$Z"
note " $( [ ${#WANT[@]} -eq 0 ] && echo "8 scenes" || echo "scenes: ${WANT[*]}" )$( [ "$AUTO" = "1" ] && echo "  (auto)" )"
[ "$AUTO" = "0" ] && note " Start your recorder now. Enter plays the first scene."
pause

for n in 1 2 3 4 5 6 7 8; do
  wanted "$n" || continue
  "scene$n"
  [ "$n" = "8" ] || pause
done

say "END"
note "  Everything above was measured from the log file given, not a fixture."
note "  Reset for the live UI take:  ./demo-reset.sh"
printf '\n'
