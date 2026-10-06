"""Build a narrated MP4 of the Aegis walkthrough. No recorder, no microphone.

    python3 demo-video/build.py                 everything
    python3 demo-video/build.py --scenes 1 2 3  only those
    python3 demo-video/build.py --no-audio      silent, fast, for layout work
    python3 demo-video/build.py --voice Rishi   a different macOS voice
    python3 demo-video/build.py --list-voices

How it works, and the one rule it follows: every number on screen is CAPTURED
from a real command at build time, never typed into the script. If a demo's
output changes, the video changes with it; nothing here can show a measurement
nobody made.

Timing is derived rather than guessed. Each paragraph is synthesised first and
its duration measured, then the typing speed for that scene is solved so the
text finishes a beat before the voice does. Edit the narration and the video
re-times itself.

Needs only ffmpeg and macOS `say`, both already present on a dev Mac. The
narration text is written out per scene, so the audio layer can be regenerated
by a better TTS (or recorded by a person) and re-stitched without touching the
frames.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from narration import SCENES  # noqa: E402

# The key lives in the repo's .env alongside every other provider key, never
# in this file: a narration script is shared far more freely than a secret.
sys.path.insert(0, str(REPO))
try:
    from app.config import load_env  # noqa: E402
    load_env()
except Exception:
    pass

OUT = HERE / "out"
WORK = OUT / "work"

W, H = 1280, 720
FPS = 30

# A terminal that reads on a phone. 20px Menlo at 1280 wide gives ~96 columns,
# which is what the demo scripts' own 74-column rules were written for.
FONT = "/System/Library/Fonts/Menlo.ttc"
FONT_SIZE = 17
LINE_H = 23
PAD_X, PAD_Y = 44, 96
MAX_LINES = (H - PAD_Y - 40) // LINE_H
MAX_COLS = 104
# Menlo advance, measured not guessed: 10.2px at size 17.
CHAR_W = FONT_SIZE * 0.6

BG = "0x0d1117"
FG = "0xc9d1d9"
ACCENT = "0x6ee7b7"      # what the voice is pointing at
TITLE = "0x79c0ff"
TAG = "0x8b949e"
RULE = "0x30363d"


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def need(tool: str) -> None:
    if shutil.which(tool) is None:
        sys.exit(f"missing required tool: {tool}")


# ---------------------------------------------------------------- capturing

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def capture(scene) -> list[str]:
    """The scene's body: real command output, or its literal `show` text."""
    if scene.command:
        proc = subprocess.run(scene.command, shell=True, cwd=REPO,
                              capture_output=True, text=True)
        text = _ANSI.sub("", proc.stdout + proc.stderr)
        lines = text.splitlines()
        if scene.grep:
            # Keep from the first matching section header to the next one that
            # is NOT wanted - sections, not grep hits, so a captured block
            # still reads like the tool's own output.
            wanted = re.compile(scene.grep)
            keep, on = [], False
            for line in lines:
                header = re.match(r"^\s*\d+\.\s+[A-Z]", line)
                if header:
                    on = bool(wanted.search(line))
                if on:
                    keep.append(line)
            lines = keep or lines
    else:
        lines = scene.show.splitlines()

    # Drop the rules the demos draw - at this font size they dominate the
    # frame, and the scene already has its own header.
    lines = [l for l in lines if not re.fullmatch(r"\s*-{10,}\s*", l)]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    out = []
    for line in lines:
        line = line.replace("\t", "    ").rstrip()
        out.append(line[:MAX_COLS])
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES - 1] + ["  ..."]
    return out


# ------------------------------------------------------------------- audio

# ElevenLabs voices worth using for a product explainer. macOS `say` is a
# formant synthesiser and sounds like one at every rate and voice; these are
# the reason the narration layer was kept swappable from the start.
EL_VOICES = {
    "daniel": "onwK4e9ZLuTAKqWW03F9",   # British, steady broadcaster
    "brian":  "nPczCjzI2devNBz1zQrb",   # American, deep and resonant
    "george": "JBFqnCBsd6RMkjVDRZzb",   # British, warm storyteller
    "eric":   "cjVigY5qzO86Huf0OWal",   # American, smooth
    "bill":   "pqHfZKP75CvOlQylNhV4",   # American, mature
}
EL_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech"


def el_keys() -> list[str]:
    """Every ElevenLabs key available, in the order they should be spent.

    A free tier is ~10k characters, which is less than this narration, so
    the builder rotates: when one key reports its quota exhausted, the next
    takes over mid-build rather than the whole run failing or silently
    dropping to `say` halfway through and changing voice on the viewer.
    """
    found, seen = [], set()
    for name in ("ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY_2",
                 "ELEVENLABS_API_KEY_3"):
        key = os.environ.get(name, "").strip()
        if key and key not in seen:
            seen.add(key)
            found.append(key)
    return found


def el_key() -> str:
    keys = el_keys()
    return keys[0] if keys else ""


def el_remaining(key: str) -> int:
    """Characters left on one key, or -1 when the account cannot be read."""
    import urllib.error
    import urllib.request
    try:
        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": key})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        return int(data.get("character_limit", 0)) - int(data.get("character_count", 0))
    except Exception:
        return -1


# Set once per build by main(); synth_elevenlabs walks forward through it.
_EL_POOL: list[str] = []
_EL_AT = 0
_TEMPO = 1.0


def synth_elevenlabs(text: str, voice: str, speed: float, dest: Path) -> float:
    """Narrate one paragraph with ElevenLabs. Returns duration, or 0 on failure.

    Returning 0 rather than raising is deliberate: the caller falls back to
    `say`, so a quota that runs out mid-build still produces a whole video
    instead of none. Before that happens the pool is walked: a 401 means
    that key is spent, so the next one takes this same paragraph over and
    the voice does not change halfway through the video.
    """
    global _EL_AT
    import urllib.error
    import urllib.request

    vid = EL_VOICES.get(voice.lower(), voice)
    body = json.dumps({
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        # speed < 1 slows delivery without the pitch artefacts that
        # resampling afterwards would introduce.
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                           "speed": speed},
    }).encode()
    mp3 = dest.with_suffix(".mp3")

    while _EL_AT < len(_EL_POOL):
        request = urllib.request.Request(
            f"{EL_ENDPOINT}/{vid}?output_format=mp3_44100_128",
            data=body,
            headers={"xi-api-key": _EL_POOL[_EL_AT],
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                mp3.write_bytes(response.read())
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            # 401 is what a spent free tier returns (quota_exceeded), so
            # move to the next key and retry THIS paragraph rather than
            # losing it. Any other status is a real error.
            if exc.code in (401, 429) and _EL_AT + 1 < len(_EL_POOL):
                _EL_AT += 1
                print(f"      key {_EL_AT} exhausted, switching to key "
                      f"{_EL_AT + 1}")
                continue
            print(f"      elevenlabs {exc.code}: {detail[:160]}")
            return 0.0
        except Exception as exc:                   # network, timeout, DNS
            print(f"      elevenlabs {exc.__class__.__name__}: {exc}")
            return 0.0
    else:
        return 0.0

    # atempo preserves pitch, so this reads as a person talking faster
    # rather than a tape played fast. Values above 2.0 need chaining, which
    # is well past anything listenable here.
    filters = ["-filter:a", f"atempo={_TEMPO:.3f}"] if abs(_TEMPO - 1.0) > 0.01 else []
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
        *filters, "-c:a", "aac", "-b:a", "128k", str(dest)])
    mp3.unlink(missing_ok=True)
    return duration(dest)


def synth(text: str, voice: str, rate: int, dest: Path,
          engine: str = "say", speed: float = 0.92) -> float:
    """Speak `text` to an m4a, and return its duration in seconds."""
    if engine == "elevenlabs":
        seconds = synth_elevenlabs(text, voice, speed, dest)
        if seconds > 0:
            return seconds
        print("      falling back to say for this scene")
    aiff = dest.with_suffix(".aiff")
    say_voice = voice if voice.lower() not in EL_VOICES else "Daniel"
    proc = sh(["say", "-v", say_voice, "-r", str(rate), "-o", str(aiff), text])
    if proc.returncode != 0:
        sys.exit(f"say failed: {proc.stderr.strip()}")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff),
        "-c:a", "aac", "-b:a", "128k", str(dest)])
    aiff.unlink(missing_ok=True)
    return duration(dest)


def duration(path: Path) -> float:
    proc = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ frames

# Every drawn string goes to its own file and is read back with
# expansion=none. Inline text= cannot carry this content safely: a percent
# sign is drawtext's expansion syntax and is eaten WHATEVER it is escaped
# with, which silently deleted "92%" and "81%" from the one frame whose
# entire argument is those two numbers. Colons, commas, brackets and
# apostrophes each need their own escape inline too; a file needs none.
_TEXTFILES: dict[str, Path] = {}


def textfile(text: str) -> Path:
    path = _TEXTFILES.get(text)
    if path is None:
        path = WORK / f"t{len(_TEXTFILES):05d}.txt"
        path.write_text(text)
        _TEXTFILES[text] = path
    return path


def draw(text: str, x, y, size: int, colour: str) -> str:
    # drawtext anchors the TOP of each call's own bounding box at y, so a
    # chunk whose tallest glyph is short - a lone comma left over after an
    # accent split - floats up to where a capital's top would be. Pinning
    # every call to the same baseline (y + size, minus this call's measured
    # ascent) keeps split segments of one line sitting on one line.
    return (f"drawtext=fontfile={FONT}:textfile={textfile(text)}"
            f":expansion=none:fontcolor={colour}:fontsize={size}"
            f":x={x}:y={y}+{size}-max_glyph_a")


def accent_spans(line: str, words: list[str]) -> list[tuple[int, int]]:
    spans = []
    low = line.lower()
    for w in words:
        start = 0
        wl = w.lower()
        while True:
            i = low.find(wl, start)
            if i < 0:
                break
            spans.append((i, i + len(w)))
            start = i + len(w)
    return spans


def frame_filters(scene, body: list[str], shown: int) -> list[str]:
    """One frame: header, then `shown` lines of the body, accents coloured."""
    f = []
    if scene.title:
        f.append(draw(scene.title, PAD_X, 34, 23, TITLE))
        if scene.tag:
            f.append(draw(scene.tag, PAD_X, 64, 16, TAG))
        f.append(f"drawbox=x={PAD_X}:y=88:w={W - 2 * PAD_X}:h=1:"
                 f"color={RULE}:t=fill")

    top = PAD_Y if scene.title else 150
    for n, line in enumerate(body[:shown]):
        if not line.strip():
            continue
        y = top + n * LINE_H
        # Every piece is positioned by COLUMN, never by letting drawtext flow
        # text. Two reasons, both learned from the frame: drawtext strips a
        # line's leading spaces, which destroys the indentation the demos use
        # to show structure; and drawing an accent on top of the full line
        # double-renders those glyphs into a smear. Monospace means column
        # times advance is exact - measured at 10.2px for size 17, i.e. 0.6.
        for a, b, colour in segments_of(line, scene.accent):
            chunk = line[a:b]
            if not chunk.strip():
                continue
            # drawtext drops a string's own leading spaces, so a chunk that
            # starts with them would be drawn one column too far left of where
            # its text actually belongs. Advance the anchor past them instead
            # and hand drawtext only the visible part.
            lead = len(chunk) - len(chunk.lstrip(" "))
            f.append(draw(chunk[lead:], f"{PAD_X}+{(a + lead) * CHAR_W:.1f}", y,
                          FONT_SIZE, colour))
    return f


def segments_of(line: str, words: list[str]) -> list[tuple[int, int, str]]:
    """Split a line into (start, end, colour) runs - accents and the rest."""
    spans = accent_spans(line, words)
    if not spans:
        return [(0, len(line), FG)]
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, cursor = [], 0
    for a, b in merged:
        if cursor < a:
            out.append((cursor, a, FG))
        out.append((a, b, ACCENT))
        cursor = b
    if cursor < len(line):
        out.append((cursor, len(line), FG))
    return out


def render_scene(scene, body: list[str], seconds: float, dest: Path) -> None:
    """Type the body out over `seconds`, then hold the full frame."""
    image = getattr(scene, "image", "")
    if image:
        # A screenshot of the real product IS the frame: shown whole for the
        # scene's length, with a slow push-in so a still reads as footage.
        src = HERE / image
        if not src.exists():
            sys.exit(f"scene {scene.key}: image missing: {src}")
        frames = int(seconds * FPS)
        zoom = 1.045  # subtle; more reads as a slide show effect
        sh(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
            "-i", str(src), "-t", f"{seconds:.3f}",
            "-vf",
            f"scale=8000:-1,zoompan=z='1+({zoom}-1)*on/{max(frames,1)}'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={W}x{H}:fps={FPS}",
            "-pix_fmt", "yuv420p", str(dest)])
        return
    real = [i for i, l in enumerate(body) if l.strip()]
    if not real:
        body, real = ["(no output captured)"], [0]

    type_s = max(0.4, seconds - scene.hold_s)
    steps = len(real)
    per = type_s / steps if steps else type_s

    segments = []
    for k, _ in enumerate(real, start=1):
        shown = real[k - 1] + 1
        hold = per if k < steps else per + scene.hold_s
        seg = WORK / f"{scene.key}-{k:03d}.mp4"
        filters = frame_filters(scene, body, shown)
        sh(["ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={BG}:s={W}x{H}:r={FPS}:d={hold:.3f}",
            "-vf", ",".join(filters) if filters else "null",
            "-pix_fmt", "yuv420p", str(seg)])
        if seg.exists():
            segments.append(seg)

    listing = WORK / f"{scene.key}.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segments))
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(listing), "-c", "copy", str(dest)])
    for s in segments:
        s.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)


# ---------------------------------------------------------------- subtitles

# Roughly one comfortable reading line; two of these per cue is the limit
# before a subtitle starts covering the screenshot it is describing.
SUB_CHARS = 42


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    out: list[str] = []
    for part in parts:
        # A very long sentence becomes two cues at a comma, rather than
        # five lines of text sitting over the product screenshot.
        if len(part) > SUB_CHARS * 2 and "," in part:
            head, _, tail = part.partition(", ")
            out.extend([head + ",", tail])
        else:
            out.append(part)
    return [p for p in out if p.strip()]


def _cues(text: str, start: float, spoken: float) -> list[tuple[float, float, str]]:
    """Split one scene's narration into timed cues across its spoken length."""
    pieces = _sentences(text)
    if not pieces:
        return []
    total_chars = sum(len(p) for p in pieces) or 1
    cues, at = [], start
    for piece in pieces:
        span = spoken * len(piece) / total_chars
        cues.append((at, at + span, textwrap.fill(piece, SUB_CHARS)))
        at += span
    return cues


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt(cues: list[tuple[float, float, str]], dest: Path) -> None:
    lines = []
    for n, (start, end, text) in enumerate(cues, start=1):
        lines.append(f"{n}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    dest.write_text("\n".join(lines))


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", nargs="*", type=int,
                    help="1-based indices; default all")
    ap.add_argument("--voice", default="Daniel")
    ap.add_argument("--rate", type=int, default=132,
                    help="words per minute; 130-140 is a measured, explanatory pace")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--no-subs", action="store_true",
                    help="skip burned-in subtitles (the .srt is still written)")
    ap.add_argument("--engine", choices=("auto", "elevenlabs", "say"),
                    default="auto",
                    help="narration engine; default uses ElevenLabs when a "
                         "key is present and falls back to say")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="ElevenLabs delivery speed (0.7-1.2)")
    ap.add_argument("--tempo", type=float, default=1.45,
                    help="post-stretch the narration; pitch is preserved")
    args = ap.parse_args()

    if args.list_voices:
        print(sh(["say", "-v", "?"]).stdout)
        return 0

    need("ffmpeg"); need("ffprobe")
    if not args.no_audio:
        need("say")
    if not Path(FONT).exists():
        sys.exit(f"font not found: {FONT}")

    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    chosen = SCENES
    if args.scenes:
        chosen = [SCENES[i - 1] for i in args.scenes if 1 <= i <= len(SCENES)]

    global _EL_POOL, _EL_AT, _TEMPO
    _TEMPO = args.tempo
    engine = args.engine
    if engine == "auto":
        engine = "elevenlabs" if el_key() else "say"
    if engine == "elevenlabs" and not el_key():
        sys.exit("--engine elevenlabs needs ELEVENLABS_API_KEY in the environment")

    if engine == "elevenlabs" and not args.no_audio:
        _EL_POOL, _EL_AT = el_keys(), 0
        budget = 0
        for n, key in enumerate(_EL_POOL, start=1):
            left = el_remaining(key)
            print(f"  key {n}: {left if left >= 0 else '?'} characters left")
            budget += max(left, 0)
        wanted = sum(len(scene.say) for scene in chosen)
        print(f"  script needs {wanted} characters, pool has {budget}")
        if budget and wanted > budget:
            print(f"  WARNING: {wanted - budget} characters short - the "
                  f"last scenes will fall back to say")

    print(f"building {len(chosen)} scene(s)"
          f"{'' if args.no_audio else f' · {engine} voice {args.voice}'}")

    parts, total = [], 0.0
    script_lines = []
    cues: list[tuple[float, float, str]] = []

    for n, scene in enumerate(chosen, start=1):
        label = scene.title or scene.key
        print(f"  [{n}/{len(chosen)}] {scene.key:18} {label}")

        body = capture(scene)

        if args.no_audio:
            # Enough to read the body at a sane pace, plus the hold.
            seconds = max(3.0, len([l for l in body if l.strip()]) * 0.5
                          + scene.hold_s)
            audio = None
        else:
            audio = WORK / f"{scene.key}.m4a"
            seconds = synth(scene.say, args.voice, args.rate, audio,
                            engine=engine, speed=args.speed)
            if seconds <= 0:
                sys.exit(f"no audio produced for {scene.key}")
            seconds += scene.hold_s

        stamp = f"{int(total // 60):02d}:{int(total % 60):02d}"
        script_lines.append(f"[{stamp}]  {label or 'TITLE'}\n"
                            f"{textwrap.fill(scene.say, 76, initial_indent='    ', subsequent_indent='    ')}\n")
        # Subtitles are split from the SAME text the voice reads, over the
        # scene's measured duration, so they cannot drift from the audio.
        # Proportional to sentence length rather than evenly spaced: a long
        # sentence genuinely takes longer to say than a short one.
        spoken = seconds - scene.hold_s
        cues.extend(_cues(scene.say, total, max(spoken, 0.5)))
        total += seconds

        silent = WORK / f"{scene.key}-v.mp4"
        render_scene(scene, body, seconds, silent)
        if not silent.exists():
            sys.exit(f"render failed for {scene.key}")

        if audio is None:
            parts.append(silent)
            continue

        merged = WORK / f"{scene.key}-av.mp4"
        # Pad the audio out to the video, never trim the video to the audio:
        # -shortest was silently deleting every scene's hold - the beat after
        # the voice stops where the frame is supposed to land.
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent),
            "-i", str(audio), "-c:v", "copy", "-af", "apad",
            "-c:a", "aac", "-t", f"{seconds:.3f}", str(merged)])
        parts.append(merged if merged.exists() else silent)

    listing = WORK / "all.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    final = OUT / "aegis-demo.mp4"
    srt = OUT / "aegis-demo.srt"
    if cues:
        write_srt(cues, srt)

    # Burn the subtitles in rather than muxing a track: a soft track is
    # ignored by most players a link is opened in, and the whole point is
    # that the words are readable without anyone turning anything on.
    subs = ""
    if cues and not args.no_subs:
        escaped = str(srt).replace("\\", "/").replace(":", "\\:")
        subs = (f"subtitles='{escaped}':force_style='FontName=Helvetica,"
                f"FontSize=17,PrimaryColour=&H00F0F6FC,OutlineColour=&HC0000000,"
                f"BorderStyle=3,Outline=0,Shadow=0,MarginV=28,Alignment=2'")

    # Re-encode on the join: the parts share a codec but concat -c copy across
    # audio boundaries drifts, and a narrated video that loses lip-sync with
    # its own text is worse than one that takes a minute longer to build.
    sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(listing),
        *(["-vf", subs] if subs else []),
        "-c:v", "libx264", "-preset", "medium",
        "-crf", "20", "-pix_fmt", "yuv420p",
        *([] if args.no_audio else ["-c:a", "aac", "-b:a", "128k"]),
        "-movflags", "+faststart", str(final)])

    if not final.exists():
        sys.exit("final concat failed")

    (OUT / "narration-script.txt").write_text(
        "AEGIS — narration script\n"
        "Timestamps are where each scene STARTS in aegis-demo.mp4.\n"
        "Re-record these lines with any voice and rebuild to swap the audio.\n\n"
        + "\n".join(script_lines))

    if not args.keep_work:
        shutil.rmtree(WORK, ignore_errors=True)

    secs = duration(final)
    size = final.stat().st_size / 1_000_000
    print(f"\n  {final}")
    print(f"  {int(secs // 60)}m {int(secs % 60)}s · {size:.1f} MB · {W}x{H}")
    print(f"  {OUT / 'narration-script.txt'}")
    if cues:
        print(f"  {srt}  ({len(cues)} cues)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
