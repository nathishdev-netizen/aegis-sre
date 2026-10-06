"""The narration script: what the voice says, and what the screen shows.

One SCENE per beat of the README's story - the problem, the six stages, the
limits. Each scene pairs spoken text with a COMMAND whose real output is
captured at build time, so the screen never shows a number nobody measured.

The voice is the product's own framing, in the README's words where they are
already the clearest version: "every observability tool tells you what
happened; none of them tell you whether it worked."

Timing is derived, never guessed: each paragraph is synthesised first, its
duration measured, and the frames stretched to fit. Edit the text and the
video re-times itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Scene:
    key: str
    title: str
    # Spoken aloud. Written for the ear: short sentences, no bullet syntax,
    # no symbols a voice cannot say.
    say: str
    # Captured live at build time. Empty means a title-card scene whose body
    # is the `show` text below.
    command: str = ""
    # Only the slice of that command's output worth seeing.
    grep: str = ""
    # A literal body, for scenes that quote the README rather than run code.
    show: str = ""
    # Held on screen after the narration ends, so a number can land.
    hold_s: float = 1.2
    tag: str = ""
    # Highlighted in the accent colour wherever they appear on screen.
    accent: list[str] = field(default_factory=list)
    # A screenshot of the real UI (path under demo-video/). When set, the
    # scene shows the product itself instead of rendered text.
    image: str = ""


SCENES: list[Scene] = [

    Scene(
        key="00-title",
        title="",
        tag="",
        say="Every observability tool tells you what happened. None of them "
            "tell you whether it worked. Here is why that matters, and what a "
            "tool built around it actually looks like.",
        show="""
                              A E G I S

         Every observability tool tells you what happened.
            None of them tell you whether it worked.


                   log intelligence that closes the loop
                   Python 3.10+   ·   no dependencies   ·   MIT
""",
        hold_s=1.6,
        accent=["A E G I S", "whether it worked"],
    ),

    Scene(
        key="01-the-order",
        title="THE PROBLEM",
        tag="one order, and every tool on the market calls it green",
        say="Start with one order, from a checkout platform of five "
            "services. This is everything it logged. Order received. Cart "
            "validated. Catalogue searched. Payment authorised, seventeen "
            "thousand four hundred and ninety. Email queued. Order completed, "
            "status two hundred, four hundred and ninety five milliseconds. "
            "No errors. Fast. Card charged, customer happy. Every dashboard "
            "you own calls this green, and every one of them is wrong.",
        command="grep 'ORD-88409' examples/shipyard.log",
        hold_s=2.2,
        accent=["200", "495ms", "order.completed", "payment.authorised"],
    ),

    Scene(
        key="02-whats-missing",
        title="THE PROBLEM",
        tag="what is not in those lines",
        say="The failure is not in those lines. It is in a line that is not "
            "there. A healthy order logs this: inventory service, stock "
            "reserved. Order eight eight four zero nine has no such line. "
            "Inventory never ran. So the money left the account, the "
            "confirmation email went out, and no warehouse knows to ship "
            "anything. Nothing to alert on, because nothing errored. The run "
            "completed. Status two hundred. The only thing that failed was "
            "the point of the order, and no tool out there has an opinion "
            "about what an order is for.",
        show="""
  a HEALTHY order:

    09:00:00.422 INFO  inventory-svc  stock.reserved order_id=ORD-88401 ...
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^

  ORD-88409 has NO such line.

    inventory-svc never ran.
    the card was charged.    the email went out.
    nothing was reserved.    nothing will ship.

  No dashboard shows this, because nothing went wrong in the way
  dashboards measure. The run completed. The status code was 200.

  The only thing that failed was THE PURPOSE OF THE ORDER -
  and no tool on the market has an opinion about what an order is for.
""",
        hold_s=2.4,
        accent=["stock.reserved", "never ran", "THE PURPOSE OF THE ORDER",
                "no error", "200"],
    ),

    Scene(
        key="03-hollow",
        title="THE PROBLEM",
        tag="it has a name, and it is countable",
        say="Aegis gives that a name: hollow. Completed cleanly, achieved "
            "nothing. Name it and you can count it, and in this one file it "
            "happens three times in twenty six orders. Compare the two tools. "
            "An error based tool reports two failures, and it is right about "
            "both, those are the declined payments. It says nothing about the "
            "three orders that took money and delivered nothing. That gap is "
            "the product.",
        show="""
  hollow   =   completed cleanly, achieved nothing


  in examples/shipyard.log  (26 orders):

      an error-based tool finds  ->   2   declined payments
      Aegis also finds           ->   3   HOLLOW orders

                                      ^
                        money taken, nothing shipped,
                             nothing alerted


  That gap is the whole product.
""",
        hold_s=2.4,
        accent=["hollow", "3", "HOLLOW", "whole product"],
    ),

    Scene(
        key="04-loop",
        title="WHAT IT DOES",
        tag="six stages, one tool, no dependencies",
        say="So, six stages. It watches your logs and your code, read only. "
            "It notices what matters using statistics, not a model. It judges "
            "every run, hollow ones included. It explains, with the lines "
            "that prove it. It fixes, with a test that had to fail first. And "
            "then it checks whether that fix actually held. Watch for that "
            "last one, because every other tool in this space stops at fix.",
        show="""
  WATCH  --->  NOTICE  --->  JUDGE  --->  EXPLAIN  --->  FIX  --->  LEARN
   logs         7 stat.      verdict      evidence +     test      did it
   & code       detectors    per run      your code      must      hold?
   read-only    statistical  incl.        mapped to      flip      grade it,
                             hollow       the line                 publish
                                                                   the rate
                                                                      |
         +------------------------------------------------------------+
         v
   what it learned changes what it does next


   Every other tool in this category stops at FIX.
""",
        hold_s=2.2,
        accent=["LEARN", "stops at FIX", "hollow"],
    ),

    Scene(
        key="04b-ui-live",
        title="THE TOOL",
        tag="the real dashboard, reading shipyard live - nothing staged",
        say="This is Aegis, pointed at that platform with one command. Down "
            "the left, the pages. Across the top, the tabs that do the work. "
            "In the middle a live brief, written as the stream moves, and you "
            "can switch it off, because it is the one thing here that is "
            "prose rather than a measurement. Below that the funnel, then run "
            "quality: twenty achieved, three hollow, two failed, one "
            "degraded. Every number got there by counting.",
        image="ui/vid/v-brief.png",
        hold_s=2.0,
    ),

    Scene(
        key="05-watch",
        title="STAGE 1 — WATCH",
        tag="no schema, no parser, no instrumentation added to your app",
        say="Stage one. No schema, no parser, nothing added to your app. "
            "Two things happen, and the order matters. First, anything "
            "sensitive is stripped before a line touches disk. That ordering "
            "is the whole privacy story: there is no unredacted copy "
            "downstream to leak, because it never existed. Second, the text "
            "collapses into shapes. A hundred and seventy five lines become "
            "nine templates. Everything above this counts templates, not "
            "lines, and that is what makes the rest affordable.",
        command="python3 -m aegis.demo.phase0 examples/shipyard.log",
        grep="2. FINGERPRINTING|3. TOP TEMPLATES",
        hold_s=2.0,
        accent=["9 distinct templates", "19.4x reduction", "26x", "24x", "21x"],
    ),

    Scene(
        key="06-counts",
        title="STAGE 1 — WATCH",
        tag="the vocabulary already shows the bug",
        say="And look at the counts, because the bug is already sitting "
            "there. Twenty six orders received. Twenty four payments "
            "authorised, the two missing ones are the declines. Twenty four "
            "completed. But only twenty one reserved stock. Twenty four minus "
            "twenty one is three. Three orders returned two hundred to a "
            "paying customer and reserved nothing. Nobody set that check up. "
            "It falls out of counting.",
        show="""
  what the templates counted:

      26x   order.received       <-  every order arrived
      26x   cart.validated
      24x   payment.authorised       (2 declined - the known failures)
      24x   order.completed      <-  returned 200 to the customer
      21x   stock.reserved       <-  ...but only 21 reserved anything

                   24 completed  -  21 reserved  =  3


  Three orders returned 200 to a paying customer
  and reserved nothing. Nobody configured this check.
  It falls out of counting.
""",
        hold_s=2.6,
        accent=["21x", "24x", "= 3", "reserved nothing"],
    ),

    Scene(
        key="07-notice",
        title="STAGE 2 — NOTICE",
        tag="seven statistical detectors, zero model calls",
        say="Stage two. Seven detectors decide what deserves attention, and "
            "every one is arithmetic: counting, ratios, medians. No model "
            "here at all, and that split is deliberate. A threshold decides "
            "whether to speak. A model, later and only once, decides what to "
            "say. Flip that and you are sending every log line to an LLM, "
            "which nobody can afford or verify at four hundred thousand lines "
            "a day.",
        command="python3 -m aegis.demo.phase2 examples/shipyard.log shipyard-video",
        grep="2. WHAT THE DETECTORS|3. THE SIGNALS",
        hold_s=2.0,
        accent=["NoveltyDetector", "signal(s)", "No model"],
    ),

    Scene(
        key="08-judge",
        title="STAGE 3 — JUDGE",
        tag="a verdict on every run, not just the failed ones",
        say="Stage three, and this is the bit no other vendor computes. To "
            "judge a run you need to know what it was meant to do, so Aegis "
            "mines that from the project's own traces. Here is the flow spec: "
            "every step, and how often it shows up. Order received, a hundred "
            "percent. Payment, ninety two. Stock reserved, eighty one. Now "
            "look at the badge on that row. Critical, human.",
        image="ui/vid/v-spec.png",
        hold_s=2.2,
    ),

    Scene(
        key="08b-ui-analyze",
        title="STAGE 3 — JUDGE",
        tag="Analyze: it reads your code, not only your logs",
        say="Stage three also reads your code. Analyze parses the source "
            "with Python's own syntax tree. No model reads your repo, because "
            "that would be unverifiable and blow past any context window. "
            "From a hundred and forty four files: twenty two ways in, twenty "
            "five dependencies, thirteen unguarded calls. And every "
            "dependency is flagged red where the code never guards it, which "
            "your logs could never have told you.",
        image="ui/vid/v-analyze.png",
        hold_s=2.6,
    ),

    Scene(
        key="09-why-human",
        title="STAGE 3 — JUDGE",
        tag="why frequency cannot find the purpose",
        say="So, that badge. Payment shows up in twenty four orders, stock "
            "in twenty one. By frequency, payment looks more essential. But "
            "what are we hunting? An order that charged the card and reserved "
            "nothing. Payment ran. Stock did not. Here is the principle, and "
            "it is the sharpest idea in the product: the step that makes a "
            "run worth having only shows up in the runs that worked, which is "
            "exactly why counting can never find it. So a human marks it, and "
            "the spec records who.",
        show="""
  by frequency alone:

      payment.authorised    92%   <-  looks MORE essential
      stock.reserved        81%   <-  looks LESS essential


  but the whole failure is: payment ran, stock did not.


  The step that makes a run worth having appears only in the runs
  that WORKED - which is exactly why frequency cannot find it.

      critical  is never auto-derived.
      a human or a model marks it, and the spec records WHICH:

          "critical": true,
          "critical_source": "human"
""",
        hold_s=2.6,
        accent=["92%", "81%", "cannot find it", "human"],
    ),

    Scene(
        key="10-verdicts",
        title="STAGE 3 — JUDGE",
        tag="every order, graded against that spec",
        say="With that spec, every order gets a verdict. Green did its job. "
            "Amber did the work but took six seconds against a one second "
            "p ninety five. Red failed. And the purple cards are hollow: "
            "eight eight four zero nine, four two two, four two six. Each one "
            "says it plainly. Completed cleanly, no errors, normal teardown, "
            "and not one of its purpose steps ran. Grep those numbers in the "
            "raw log and check them yourself.",
        image="ui/vid/v-runs.png",
        hold_s=2.6,
    ),

    Scene(
        key="10b-ui-incidents",
        title="STAGE 3 — JUDGE",
        tag="one incident, not forty alerts",
        say="Signals do not turn into forty alerts. The ones belonging to a "
            "single story become one incident. Members listed, first one "
            "tagged ranked cause, and underneath, why it ranked: earliest "
            "onset, both sharing a trace, ranked on timing alone. It shows "
            "its working instead of just asserting. Then memory: a past "
            "incident at zero point nine similarity, carrying the words "
            "precedent, not conclusion.",
        image="ui/vid/v-incident.png",
        hold_s=2.2,
    ),

    Scene(
        key="11-explain",
        title="STAGE 4 — EXPLAIN",
        tag="one governed model call, and every citation checked",
        say="Stage four is the first time a model runs at all, under three "
            "constraints that are enforced in code. It is budgeted, because "
            "an outage is exactly when an ungoverned reasoning layer would "
            "spend the most. It is audited, and the record is written before "
            "the call, not after. And it only ever sees redacted text, "
            "because there is no other version to send.",
        command="python3 -m aegis.demo.explain examples/shipyard.log --n 1",
        grep="INC-|confidence|evidence:|now:|later:|calls actually made",
        hold_s=2.4,
        accent=["grounded", "confidence=", "evidence:", "calls actually made: 1"],
    ),

    Scene(
        key="11b-ui-explain",
        title="STAGE 4 — EXPLAIN",
        tag="one call, and every citation checked against the evidence",
        say="Hit Explain, and one governed call runs. The answer comes back "
            "tagged grounded, high. Payment was declined for insufficient "
            "funds, which made the checkout API record an order failure. And "
            "underneath, the two log lines it rests on, quoted exactly. That "
            "check is not decoration. Every citation gets matched against the "
            "evidence the model was handed. Anything it made up is dropped "
            "and counted, and if nothing survives, it reads unverified and "
            "confidence drops to low. The research is blunt here: a good "
            "explanation makes you trust wrong answers just as much as right "
            "ones. Only a checkable source fixes that.",
        image="ui/vid/v-explain.png",
        hold_s=2.6,
    ),

    Scene(
        key="12-fix",
        title="STAGE 5 — FIX",
        tag="a log line has no pointer to the code that wrote it",
        say="Stage five has a hard problem first. A log line has no pointer "
            "to the code that wrote it. The two worlds share no identifier. "
            "So Aegis parses your source and matches format strings back to "
            "the lines they produced. That bridge is what lets it say "
            "reserve dot py, line eighty nine, instead of something went "
            "wrong somewhere. And it pulls double duty: the file it mapped to "
            "becomes the only file a patch is allowed to touch. Then write a "
            "test that fails. If it passes before the patch, the diagnosis "
            "was wrong, and it stops.",
        show="""
  the bridge:

    log line    "stock.reserved order_id=ORD-88401 sku=SKU-165"
                                |
                       parse the source with ast,
                       match the format string
                                v
    source      inventory-svc/reserve.py:89  in reserve_stock()
                  calls   http://inventory:8200/reserve
                  NOT guarded, NO timeout


  then, in order:

    1  write a reproducer     ->  it MUST FAIL now
          passes already?         diagnosis was wrong - STOP
    2  write a minimal patch  ->  <= 40 lines, never a test file
    3  reproducer must PASS
    4  your own suite still green, or it is REVERTED
    5  a draft bundle - never a write to your repo


  The mapped file is also the ONLY file the patch may touch.
""",
        hold_s=2.8,
        accent=["reserve.py:89", "MUST FAIL", "STOP", "REVERTED",
                "ONLY file"],
    ),

    Scene(
        key="12a-fix-blocked",
        title="STAGE 5 — FIX",
        tag="a real crash, a real repo, and a refusal",
        say="Here it is on a real service with a real crash. Aegis mapped "
            "the evidence to source, wrote a reproducer, proved it failed, "
            "drafted a patch. Then the gate rejected it. Two hunks removed "
            "the same line, so the second could never apply. Blocked. Aegis "
            "proposes only what it has proven, and it could not prove this "
            "one.",
        image="ui/vid/v-fix.png",
        hold_s=2.8,
    ),

    Scene(
        key="12b-ui-improve",
        title="STAGE 5 — FIX",
        tag="when nothing maps, it names the line to add",
        say="And when the trail goes cold, it says so. Research found "
            "twenty seven percent of failures could not be diagnosed, because "
            "the evidence was never captured. No model recovers a log line "
            "nobody wrote. So this page tells you what to log next: the gaps "
            "the analysis actually hit, each with the measurement that proves "
            "it.",
        image="ui/vid/v-improve.png",
        hold_s=2.4,
    ),

    Scene(
        key="13-learn",
        title="STAGE 6 — LEARN",
        tag="the stage every other tool skips",
        say="Stage six is the one nobody else has. A survey of the open "
            "source field turned up nothing that asks whether a fix held, "
            "nothing that learns from recorded outcomes, nothing that reports "
            "its own accuracy. The one project with a real learning loop "
            "marks that feature closed source in its own docs. A vendor "
            "picking this as its moat is the best evidence going of which "
            "part is worth having.",
        show="""
  surveyed, October 2026:

      SigNoz      32.3k stars        Keep        12.4k
      k8sgpt       8.2k              HolmesGPT    3.5k  (CNCF Sandbox)
      Robusta      3.1k

  none of them:
      - ask whether a fix HELD
      - learn from recorded outcomes
      - report their own accuracy

  the one project with a real learning loop marks it

      Open Source: (X)

  in its own documentation.

  A vendor choosing THIS capability as its moat is the best
  available evidence of which part is worth having.
""",
        hold_s=2.6,
        accent=["HELD", "Open Source: (X)", "moat"],
    ),

    Scene(
        key="14-memory",
        title="STAGE 6 — LEARN",
        tag="resolving is archiving - nobody has to remember",
        say="Every resolved incident gets archived automatically, as a "
            "structured signature rather than prose. Resolving is archiving, "
            "so nobody has to remember. When something similar turns up "
            "later, memory hands the investigation its own history before any "
            "hypothesis forms. And a remembered wrong diagnosis is kept just "
            "as carefully as a right one, because it stops the same bad "
            "answer coming back with a precedent's authority.",
        command="python3 -m aegis.demo.memory examples/shipyard.log --no-llm",
        grep="1. THE ARCHIVE|2. THE MATCH|3. THE PATTERN",
        hold_s=2.4,
        accent=["archived on resolution", "similar past incident",
                "precedent, not conclusion", "outcome:"],
    ),

    Scene(
        key="15-held",
        title="STAGE 6 — LEARN",
        tag="did the fix actually hold?",
        say="And then the measurement nobody does. Mark a fix as working "
            "and it freezes the numbers to judge it by, then measures them "
            "again later. Two measures, because one does not generalise. "
            "Latency answers a slow flow. But for an error, or a run of "
            "hollow checkouts, the timing never moves, so the thing that "
            "always works is whether the incident's own templates fired "
            "again. Held, recurred with the count that proves it, or too "
            "early, which says so instead of guessing.",
        show="""
    mark a fix worked
            |
            v
    baselines + template counts FROZEN at that moment
            |
            v
    5 minutes later:  re-measure the same numbers
            |
            v
    recurred?  ->  downgrade it, with the count that proves it


    held        none of its templates has fired since the fix
    recurred    T-65b79888 has fired 14 more time(s) since the fix
    too-early   fewer than 8 new runs - not enough to mean anything
    worse       the operation got SLOWER after the fix


    The outcome label says what a human BELIEVED.
    This says what the measurements DID - and they can disagree.
""",
        hold_s=2.8,
        accent=["FROZEN", "recurred", "held", "can disagree"],
    ),

    Scene(
        key="16-honest",
        title="HONEST LIMITS",
        tag="what it does not do",
        say="Finally, what it does not do. It never merges, and that is not "
            "a setting: the capability does not exist at any tier. It never "
            "edits a test to make a patch pass. It never writes to the repo "
            "it is diagnosing. One project's data is one file on disk. And "
            "the chatbot answers about the run in front of you, not the whole "
            "archive. That is not built yet, and the docs say so.",
        show="""
  never merges            can_merge() returns False - at ANY tier,
                          at ANY confidence. Not a setting.

  never edits a test      a patch touching a test file is rejected
                          before it is even considered.

  never writes to         a fix is a DRAFT bundle. applying it
  your repo               is a human act.

  per-project isolation   one SQLite file per project. no shared
                          table for a WHERE clause to get wrong.

  logs stay yours         lines are never copied. only derived
                          numbers are stored.

  not built yet           questions across the whole archive;
                          the chatbot answers about the latest run.
""",
        hold_s=2.6,
        accent=["never", "False", "DRAFT", "not built yet"],
    ),

    Scene(
        key="17-end",
        title="",
        tag="",
        say="One sentence to finish. Your dashboards will keep telling you "
            "checkout returns two hundred in four hundred and ninety five "
            "milliseconds, and they are right. Aegis tells you three of those "
            "orders never reserved stock, which line of code did it, and "
            "whether the fix you shipped held.",
        show="""
   Your dashboards keep telling you:

       checkout-api returns 200 in 495ms.


   Aegis tells you:

       3 of those orders never reserved stock,
       which line of code is responsible,
       and whether the fix you shipped HELD.



                              A E G I S

          python3 run.py       ->   http://localhost:8600
          ./demo.sh            ->   this walkthrough, live
          379 tests, 30 suites, no dependencies
""",
        hold_s=3.0,
        accent=["A E G I S", "HELD", "never reserved stock"],
    ),
]
