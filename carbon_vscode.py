"""VS Code Copilot Chat: per-request token counts from local chat session logs.

VS Code keeps one directory per workspace under
~/Library/Application Support/Code/User/workspaceStorage/<hash>/ (on Linux,
~/.config/Code/User/...). Its workspace.json names the folder ("folder", a
file URI) or a multi-root workspace file ("workspace", whose "folders" list
holds the paths). chatSessions/*.jsonl are operation logs, not snapshots:

    {"kind": 0, "v": {...session, "requests": [...]}}   initial snapshot
    {"kind": 1, "k": [path...], "v": value}             set the value at path
    {"kind": 2, "k": [path...], "v": [items], "i": n}   truncate the list at
                                                         path to n (if i is
                                                         given), then append

Token counts are set by later kind-1 patches, often more than once as a
response streams, so the log has to be replayed; matching lines would count
a request several times. A truncated request (an undone or retried turn)
was still sent, so every request object the replay ever creates is kept,
and requests are deduplicated on requestId across all files.

Per request, the log has promptTokens (the prompt of the request's final
model call, i.e. the context size), completionTokens (output summed over the
request's model calls) and copilotCredits; older requests have no counts.
modelId is usually "copilot/auto", so the model actually used is read from
result.metadata.resolvedModel, else the tool-call rounds, else the
"<Model> . <n> credits" details string.
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

import carbon_common as cc
import token_carbon as tc

STORAGE = Path.home() / "Library" / "Application Support" / "Code" / "User"

# Energy class (low, mid, high) as TokenClimate families, by model id prefix;
# the first match wins. Claude models use their own family. The GPT and MAI
# rows are assumptions: neither OpenAI nor Microsoft publishes per-token energy
# or model size, so each is placed by its price and positioning against the
# Claude tiers (GPT-5.6 Sol > Terra > Luna; Codex and MAI Flash as smaller
# coding models), with a range spanning the neighbouring tiers.
MODEL_CLASSES = [
    ("claude-", None, "TokenClimate family of the Claude model"),
    ("gpt-5.6-sol", ("sonnet", "opus", "fable"), "assumed Opus class: largest GPT-5.6 tier"),
    ("gpt-5.6-terra", ("sonnet", "opus", "opus"), "assumed Opus class: middle GPT-5.6 tier"),
    ("gpt-5.5", ("sonnet", "opus", "opus"), "assumed Opus class: GPT-5.5 flagship"),
    ("gpt-5.6-luna", ("haiku", "sonnet", "opus"), "assumed Sonnet class: smallest GPT-5.6 tier"),
    ("gpt-5.3-codex", ("haiku", "sonnet", "opus"), "assumed Sonnet class: coding model"),
    ("gpt-5", ("haiku", "sonnet", "opus"), "assumed Sonnet class: other GPT-5.x"),
    ("mai-", ("haiku", "haiku", "sonnet"), "assumed Haiku class: 'Flash' tier"),
    ("copilot/auto", ("haiku", "sonnet", "opus"), "assumed Sonnet class: model not recorded"),
    ("", ("sonnet", "opus", "fable"), "unrecognized: counted as Opus, like token_carbon"),
]

# Costing parameters. cached_fraction is the share of prompt tokens costed as
# cache reads (TokenClimate 0.08 x input) rather than uncached input: the logs
# have no cache split, so 0 (all uncached) is the stated default. promptTokens
# is the final call's prompt only; the high bound instead takes a request's
# prompt tokens as promptTokens x (rounds + 1) / 2, i.e. a context growing
# linearly over its model calls, all uncached. Requests with no counts are
# given the median counts of counted requests for the same model (all models
# if fewer than impute_min have counts) in the mid and high bounds, and zero
# in the low bound.
PARAMS = {"cached_fraction": 0.0, "impute_min": 5}


def file_path(uri):
    return unquote(urlparse(uri).path) if uri.startswith("file:") else uri


def workspace_folders(ws_dir):
    """Folder paths of a workspaceStorage entry (several for a multi-root workspace)."""
    try:
        meta = json.loads((ws_dir / "workspace.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if "folder" in meta:
        return [file_path(meta["folder"]).rstrip("/")]
    if "workspace" in meta:
        wfile = Path(file_path(meta["workspace"]))
        try:
            folders = json.loads(wfile.read_text()).get("folders", [])
        except (OSError, json.JSONDecodeError):
            return []
        return [str((wfile.parent / f["path"]).resolve()) if not f["path"].startswith("/")
                else f["path"].rstrip("/") for f in folders if "path" in f]
    return []


def replay(path):
    """Every request object a chat session log creates, in order."""
    state, seen, ids = None, [], set()

    def keep(items):
        for q in items:
            if isinstance(q, dict) and id(q) not in ids:
                ids.add(id(q))
                seen.append(q)

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                op = json.loads(line)
            except json.JSONDecodeError:
                continue
            if op.get("kind") == 0:
                state = op.get("v") or {}
                keep(state.get("requests", []))
                continue
            if state is None or not op.get("k"):
                continue
            obj = state
            try:
                for p in op["k"][:-1]:
                    obj = obj[p]
                last = op["k"][-1]
                if op["kind"] == 1:
                    obj[last] = op.get("v")
                elif op["kind"] == 2:
                    arr = obj.setdefault(last, []) if isinstance(obj, dict) else obj[last]
                    if "i" in op:
                        del arr[op["i"]:]
                    arr.extend(op.get("v") or [])
            except (KeyError, IndexError, TypeError):
                continue
            if op["k"][0] == "requests":
                keep(state.get("requests", []))
    return seen


def resolved_model(q):
    md = (q.get("result") or {}).get("metadata") or {}
    if md.get("resolvedModel"):
        return md["resolvedModel"]
    rounds = [r.get("modelId") for r in md.get("toolCallRounds") or [] if r.get("modelId")]
    if rounds:
        return rounds[-1]
    details = (q.get("result") or {}).get("details") or ""
    if details:
        return details.split(" • ")[0].strip().lower().replace(" ", "-")
    return q.get("modelId") or "unknown"


def model_class(model):
    """(low, mid, high) TokenClimate families and the reason, from MODEL_CLASSES."""
    m = model.lower()
    for prefix, families, note in MODEL_CLASSES:
        if m.startswith(prefix):
            if families is None:
                f = tc.model_family(m)
                return (f, f, f), note
            return families, note
    return MODEL_CLASSES[-1][1:]


def request_record(q, session, workspace, folders):
    result = q.get("result") or {}
    md = result.get("metadata") or {}
    prompt = q.get("promptTokens", md.get("promptTokens"))
    completion = q.get("completionTokens")
    output = completion if completion is not None else md.get("outputTokens")
    return {
        "session": session, "workspace": workspace, "folders": folders,
        "ts": q.get("timestamp", 0), "day": cc.iso_day(q.get("timestamp", 0)),
        "model_id": q.get("modelId", ""), "model": resolved_model(q),
        "prompt": prompt or 0, "output": output or 0,
        "has_tokens": prompt is not None,
        "rounds": len(md.get("toolCallRounds") or []),
        "credits": q.get("copilotCredits") or 0.0,
        "elapsed_ms": q.get("elapsedMs") or (result.get("timings") or {}).get("totalElapsed") or 0,
    }


def collect(folders, storage=STORAGE):
    """Records for workspaces under the given folders, plus monthly totals for all.

    Returns {"vscode_copilot": {requestId: record}, "vscode_months":
    {"YYYY-MM|workspace": totals}}. The monthly totals cover every workspace,
    so a repository's share of account-wide Copilot billing can be computed.
    """
    root = Path(storage).expanduser() / "workspaceStorage"
    wanted = [str(Path(f).expanduser()).rstrip("/") for f in folders]
    records, months = {}, {}
    if not root.is_dir():
        cc.warn(f"{root} not found; no VS Code Copilot records")
        return {"vscode_copilot": records, "vscode_months": months}
    for ws in sorted(p for p in root.iterdir() if (p / "chatSessions").is_dir()):
        ws_folders = workspace_folders(ws)
        match = any(f == w or f.startswith(w + "/") for f in ws_folders for w in wanted)
        seen = {}
        for path in sorted((ws / "chatSessions").glob("*.jsonl")):
            for q in replay(path):
                rid = q.get("requestId")
                if not rid:
                    continue
                r = request_record(q, path.stem, ws.name, ws_folders)
                if rid in seen:
                    cc.merge_records(seen, {rid: r})
                else:
                    seen[rid] = r
        for rid, r in seen.items():
            m = months.setdefault(f"{r['day'][:7]}|{ws.name}", {
                "month": r["day"][:7], "workspace": ws.name, "repo": match,
                "requests": 0, "credits": 0.0, "prompt": 0, "output": 0})
            m["requests"] += 1
            m["credits"] += r["credits"]
            m["prompt"] += r["prompt"]
            m["output"] += r["output"]
            if match:
                records[rid] = r
    return {"vscode_copilot": records, "vscode_months": months}


def cost(records, classes=None, params=PARAMS):
    """Costed items, one per request, each with (low, mid, high) kWh and kg."""
    counted = [r for r in records.values() if r.get("has_tokens")]
    by_model = defaultdict(list)
    for r in counted:
        by_model[r["model"]].append(r)

    def medians(rs):
        if not rs:
            return 0, 0
        return statistics.median(r["prompt"] for r in rs), statistics.median(r["output"] for r in rs)

    overall = medians(counted)
    cf = params["cached_fraction"]
    items = []
    for rid, r in sorted(records.items(), key=lambda kv: kv[1]["ts"]):
        imputed = not r.get("has_tokens")
        if imputed:
            peers = by_model.get(r["model"], [])
            prompt, output = medians(peers) if len(peers) >= params["impute_min"] else overall
        else:
            prompt, output = r["prompt"], r["output"]
        fams, _ = model_class(r["model"])
        mid_tokens = {"input": prompt * (1 - cf), "cache_read": prompt * cf, "output": output}
        high_tokens = {"input": prompt * (max(r.get("rounds", 0), 1) + 1) / 2, "output": output}
        lo = (0.0, 0.0) if imputed else cc.llm_cost(fams[0], mid_tokens)
        mid = cc.llm_cost(fams[1], mid_tokens)
        hi = cc.llm_cost(fams[2], high_tokens)
        items.append({
            "source": "vscode_copilot", "id": rid, "day": r["day"], "label": r["model"],
            "kind": "local chat (imputed tokens)" if imputed else "local chat",
            "branch": None, "class": "dev",
            "tokens": {"prompt": prompt, "output": output},
            "kwh": cc.triple(lo[0], mid[0], hi[0]), "kg": cc.triple(lo[1], mid[1], hi[1]),
        })
    return items
