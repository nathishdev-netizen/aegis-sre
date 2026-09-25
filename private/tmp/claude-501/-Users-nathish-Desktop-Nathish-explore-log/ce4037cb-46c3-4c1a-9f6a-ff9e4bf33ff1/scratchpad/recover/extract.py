"""Rebuild the repo from the session transcripts.

Every file was created either by a Write/Edit tool call or by a heredoc
inside a Bash command. Both are recorded verbatim in the JSONL, in order,
so replaying them newest-last reconstructs the final state of each file.
"""
import json, glob, os, re, sys, collections

TRANSCRIPTS = "/Users/nathish/.claude/projects/-Users-nathish-Desktop-Nathish-explore-log"
REPO = "/Users/nathish/Desktop/Nathish/explore/log"

def rows():
    """Every tool_use, in chronological order across all sessions."""
    items = []
    for path in glob.glob(os.path.join(TRANSCRIPTS, "*.jsonl")):
        with open(path, errors="replace") as fh:
            for n, line in enumerate(fh):
                try: row = json.loads(line)
                except Exception: continue
                ts = row.get("timestamp") or ""
                msg = row.get("message") or {}
                c = msg.get("content")
                if not isinstance(c, list): continue
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        items.append((ts, path, n, part))
    items.sort(key=lambda t: (t[0], t[1], t[2]))
    return items

# cat > path <<'MARK' ... MARK   (also >> for appends)
HEREDOC = re.compile(
    r"cat\s*(>>?)\s*(['\"]?)([\w./\- ]+)\2\s*<<\s*(['\"]?)([A-Za-z_][\w]*)\4\n(.*?)\n\5(?:\n|$)",
    re.S)

def norm(p):
    p = p.strip().strip("'\"")
    if p.startswith(REPO + "/"): p = p[len(REPO) + 1:]
    return p.lstrip("./")

def main():
    latest = {}          # relpath -> content
    stats = collections.Counter()
    for ts, _src, _n, part in rows():
        name = part.get("name"); inp = part.get("input") or {}
        if name == "Write":
            fp = inp.get("file_path") or ""
            if REPO in fp:
                latest[norm(fp)] = inp.get("content") or ""
                stats["write"] += 1
        elif name in ("Edit", "MultiEdit"):
            fp = inp.get("file_path") or ""
            if REPO not in fp: continue
            rel = norm(fp)
            edits = inp.get("edits") or [inp]
            for e in edits:
                old, new = e.get("old_string"), e.get("new_string")
                if old is None or new is None: continue
                if rel in latest and old in latest[rel]:
                    latest[rel] = (latest[rel].replace(old, new)
                                   if not e.get("replace_all")
                                   else latest[rel].replace(old, new))
                    stats["edit_applied"] += 1
                else:
                    stats["edit_unanchored"] += 1
        elif name == "Bash":
            cmd = inp.get("command") or ""
            for m in HEREDOC.finditer(cmd):
                op, _q, path, _q2, _mark, body = m.groups()
                rel = norm(path)
                if not rel or rel.startswith("/"): continue
                if op == ">>" and rel in latest:
                    latest[rel] += "\n" + body + "\n"
                    stats["heredoc_append"] += 1
                else:
                    latest[rel] = body + "\n"
                    stats["heredoc"] += 1
    print("recovered files:", len(latest))
    for k, v in stats.most_common(): print(f"   {k}: {v}")
    out = sys.argv[1]
    for rel, content in sorted(latest.items()):
        dest = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as fh: fh.write(content)
    print("written to", out)

main()
