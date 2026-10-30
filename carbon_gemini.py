"""Gemini in Google Antigravity: per-call token counts from local conversation DBs.

Antigravity (the app, ~/.gemini/antigravity, and the CLI,
~/.gemini/antigravity-cli) keeps conversation_summaries.db, whose table
conversation_summaries has one row per conversation (conversation_id, title,
workspace_uris as a JSON list of file URIs, parent_conversation_id for a
subagent, last_modified_time), and conversations/<id>.db per conversation.
In a conversation DB, gen_metadata(idx, data) holds one protobuf per model
call, read here with a minimal wire-format reader (no protobuf dependency).
Field 1 of it is the call:

    1.19  model id, e.g. "gemini-4-argon", "gemini-3.8-flash-tiered"
    1.4   usage: .2 uncached input, .5 cached-read input (absent on a
          conversation's first call), .3 output, .11 response id; .9 and
          .10 split the output (.3 = .9 + .10 on every call seen, so .10,
          probably thinking, is already in .3); .1 is the model enum
    1.9.10.1 / 1.9.10.4  context used / context window

gen_metadata field 4 matches field 12 of a row of steps.metadata, whose
field 1 is that step's timestamp (seconds); a call without a matching step
takes the conversation's last_modified_time.

A conversation's project is, in order: the repository named in its title
(the argon-review script titles its sessions "Argon Review: owner/repo#N"
and "Argon Adversarial Audit: owner/repo (date)"); the git repository of its
first workspace folder (git remote of the path, or the folder name under
~/code), or the folder itself if it exists outside any repository; its
parent conversation's project (a deleted subagent worktree); else
"unattributed".

Google publishes no per-token energy. Its per-prompt figure, 0.24 Wh for
the median Gemini Apps text prompt (Elsworth et al. 2025, arXiv:2508.15734),
is a cross-check only: agentic calls carry contexts of 10^4-10^5 tokens and
cost far more than a median chat prompt.
"""

import fnmatch
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import carbon_common as cc

ROOTS = [Path.home() / ".gemini" / "antigravity", Path.home() / ".gemini" / "antigravity-cli"]
CODE_DIR = Path.home() / "code"
UNATTRIBUTED = "unattributed"

# Energy class (low, mid, high) as TokenClimate families, by model id glob; the
# first match wins. Assumptions, placed by tier as for the GPT rows in
# carbon_vscode: Argon as the Gemini 4 flagship (Opus class), Flash models as
# the small tier (Haiku class), with a range over the neighbouring tiers.
MODEL_CLASSES = [
    ("gemini-4-argon*", ("sonnet", "opus", "opus"), "assumed Opus class: Gemini 4 flagship"),
    ("gemini-*-flash*", ("haiku", "haiku", "sonnet"), "assumed Haiku class: Flash tier"),
    ("*", ("sonnet", "opus", "fable"), "unrecognized: counted as Opus, like token_carbon"),
]

# Cache reads at this multiple of input energy (low, mid, high), as for Claude.
PARAMS = {"cache_read_scale": (0.05, 0.08, 0.20)}

TITLE_REPO = re.compile(r"^Argon [\w ]*?: ([\w.-]+/[\w.-]+)(?:#(\d+))?")
GIT_SLUG = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def varint(b, i):
    n = shift = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if x < 0x80:
            return n, i


def fields(b):
    """[(field number, value)] of a protobuf message: ints, or bytes for wire types 1, 2, 5."""
    i, out = 0, []
    while i < len(b):
        key, i = varint(b, i)
        f, wire = key >> 3, key & 7
        if wire == 0:
            v, i = varint(b, i)
        elif wire == 1:
            v, i = b[i:i + 8], i + 8
        elif wire == 5:
            v, i = b[i:i + 4], i + 4
        elif wire == 2:
            n, i = varint(b, i)
            v, i = bytes(b[i:i + n]), i + n
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((f, v))
    return out


def get(b, *path):
    """The first value at a field path, or None."""
    for p in path:
        b = next((v for f, v in fields(b) if f == p), None)
        if b is None:
            return None
    return b


def call_record(data, conversation, root, step_ts, fallback_ts):
    """A record for one gen_metadata blob, or None if it has no usage."""
    call = get(data, 1)
    usage = get(call, 4) if call else None
    if not isinstance(usage, bytes):
        return None
    u = dict(fields(usage))
    key = get(data, 4)
    ts = step_ts.get(key, fallback_ts)
    model = get(call, 19)
    return {
        "conversation": conversation, "root": root,
        "response": u.get(11, b"").decode(errors="replace"),
        "ts": ts, "day": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(),
        "model": model.decode(errors="replace") if isinstance(model, bytes) else "unknown",
        "input": u.get(2, 0), "cache_read": u.get(5, 0), "output": u.get(3, 0),
        "thinking": u.get(10, 0), "context": get(call, 9, 10, 1) or 0,
    }


def summaries(roots):
    """{conversation id: summary row} from every root; non-empty fields win."""
    out = {}
    for root in roots:
        db = Path(root).expanduser() / "conversation_summaries.db"
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute(
                    "select conversation_id, title, workspace_uris, parent_conversation_id, "
                    "last_modified_time from conversation_summaries"):
                s = out.setdefault(row["conversation_id"], {})
                s.update({k: row[k] for k in row.keys() if row[k] not in (None, "", "[]")})
        finally:
            con.close()
    return out


def git_slug(path):
    """owner/repo of a checkout's origin, or None."""
    try:
        p = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = GIT_SLUG.search(p.stdout.strip()) if p.returncode == 0 else None
    return m.group(1) if m else None


def workspace_project(uris, code_dir=CODE_DIR):
    """The project of a conversation's first workspace folder, or None."""
    try:
        uris = json.loads(uris or "[]")
    except json.JSONDecodeError:
        return None
    for uri in uris:
        path = unquote(urlparse(uri).path) if uri.startswith("file:") else uri
        exists = Path(path).is_dir()
        slug = git_slug(path) if exists else None
        if slug:
            return slug
        if Path(path).is_relative_to(code_dir) and Path(path) != code_dir:
            return Path(path).relative_to(code_dir).parts[0]
        if exists:
            return path.rstrip("/")  # a folder outside any repository, e.g. a scratch space
    return None


def project_of(cid, summ, code_dir=CODE_DIR, _seen=None):
    """(project, PR number or None) of a conversation; see the module docstring."""
    s = summ.get(cid, {})
    m = TITLE_REPO.match(s.get("title", ""))
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else None
    proj = workspace_project(s.get("workspace_uris"), code_dir)
    if proj:
        return proj, None
    seen = (_seen or set()) | {cid}
    parent = s.get("parent_conversation_id")
    if parent and parent not in seen:
        return project_of(parent, summ, code_dir, seen)
    return UNATTRIBUTED, None


def summary_ts(s):
    t = s.get("last_modified_time")
    try:
        return datetime.fromisoformat(t).timestamp() if t else 0
    except ValueError:
        return 0


def collect(roots=ROOTS, code_dir=CODE_DIR):
    """{"gemini": {response id: record}} for every conversation in every root.

    All projects are kept; cost() decides which count. The ledger max-merge
    keeps records whose conversation DB has since been deleted.
    """
    summ = summaries(roots)
    projects = {}
    records = {}
    for root in roots:
        conv_dir = Path(root).expanduser() / "conversations"
        if not conv_dir.is_dir():
            cc.warn(f"{conv_dir} not found; no Gemini records from it")
            continue
        for db in sorted(conv_dir.glob("*.db")):
            cid = db.stem
            if cid not in projects:
                projects[cid] = project_of(cid, summ, Path(code_dir))
            project, pr = projects[cid]
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                step_ts = {}
                for (md,) in con.execute("select metadata from steps where metadata is not null"):
                    k, t = get(md, 12), get(md, 1, 1)
                    if k is not None and t is not None:
                        step_ts[k] = t
                rows = con.execute("select idx, data from gen_metadata").fetchall()
            except sqlite3.Error as e:
                cc.warn(f"{db.name}: {e}")
                continue
            finally:
                con.close()
            fallback = summary_ts(summ.get(cid, {})) or db.stat().st_mtime
            for idx, data in rows:
                r = call_record(data, cid, Path(root).name, step_ts, fallback)
                if r is None:
                    continue
                r.update(project=project, pr=pr)
                records[r["response"] or f"{cid}:{idx}"] = r
    return {"gemini": records}


def model_class(model):
    """(low, mid, high) TokenClimate families and the reason, from MODEL_CLASSES."""
    m = model.lower()
    for pattern, families, note in MODEL_CLASSES:
        if fnmatch.fnmatch(m, pattern):
            return families, note
    return MODEL_CLASSES[-1][1:]


def project_class(project, classes, include):
    """science (by the classes' dir globs), the class of an included project,
    'other' for any other project, or 'unattributed'."""
    if project == UNATTRIBUTED:
        return UNATTRIBUTED
    cls = cc.classify(classes, dir=project)
    if cls == "science":
        return cls
    name = project.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(project, g) or fnmatch.fnmatch(name, g) for g in include):
        return cls
    return "other"


def cost(records, classes=None, include=(), repo=None, params=PARAMS):
    """Costed items, one per model call, each with (low, mid, high) kWh and kg."""
    items = []
    for rid, r in sorted(records.items(), key=lambda kv: kv[1]["ts"]):
        fams, _ = model_class(r["model"])
        tokens = {"input": r["input"], "cache_read": r["cache_read"], "output": r["output"]}
        e = [cc.llm_cost(f, tokens, cache_read_scale=s)
             for f, s in zip(fams, params["cache_read_scale"])]
        items.append({
            "source": "gemini", "id": rid, "day": r["day"], "label": r["model"],
            "kind": "model calls", "branch": None,
            "pr": r.get("pr") if repo and r.get("project") == repo else None,
            "class": project_class(r.get("project", UNATTRIBUTED), classes or {}, include),
            "tokens": tokens, "calls": 1,
            "kwh": cc.triple(*(x[0] for x in e)), "kg": cc.triple(*(x[1] for x in e)),
        })
    return items
