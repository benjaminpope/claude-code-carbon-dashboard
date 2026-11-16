"""Token counts per model and scope group, for a paper to cite.

`export_counts` turns the Claude Code history and the Gemini (Antigravity)
records into per-session rows plus aggregates; `merge_exports` combines the
exports of several machines, deduplicating by session.

Scope comes from a local TOML file that is never committed (default
~/.config/claude-carbon/scope.toml, or $CLAUDE_CARBON_SCOPE), so no private
repository name appears in this repository. See scope.example.toml:

    roots = ["code"]            # directory names the repositories live under
    [groups]
    core = ["repo-a", "repo-b"]  # group name -> repository names

A Claude record is in a group when its project directory is
<anything>-<root>-<repo>, optionally followed by --claude-worktrees-<name>
(a Claude Code worktree). A Gemini record is in a group when the last path
component of its project (owner/repo or a folder name) is the repository
name. Records matching no group fall into "(out of scope)", which is reported
as totals only and never enters "combined".

The output carries no titles, paths or session ids. Session rows are keyed by
the first 16 hex digits of a SHA-256 of the source and session id, which
exists only so that merge_exports can deduplicate; the merged file keeps no
rows, and `--aggregate-only` drops them from an export too.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = "claude-carbon-counts/1"
COMPONENTS = ("input", "cache_read", "cache_write", "output")
OUT_OF_SCOPE = "(out of scope)"
DEFAULT_SCOPE = Path.home() / ".config" / "claude-carbon" / "scope.toml"
# What each source records; the others stay zero.
RECORDED = {"claude": list(COMPONENTS), "gemini": ["input", "cache_read", "output"]}
PROJECT_DIR_WORKTREE = r"(?:--claude-worktrees-.*)?$"


def load_scope(path=None):
    """{"roots": [...], "groups": {name: [repo, ...]}} from a TOML file, or None if absent."""
    path = Path(path or os.environ.get("CLAUDE_CARBON_SCOPE") or DEFAULT_SCOPE).expanduser()
    if not path.exists():
        return None
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            sys.exit("reading the scope file needs Python 3.11+ (or tomli)")
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    groups = {str(k): [str(r) for r in v] for k, v in cfg.get("groups", {}).items()}
    if not groups:
        sys.exit(f"{path}: no [groups] table")
    return {"roots": [str(r) for r in cfg.get("roots", ["code"])], "groups": groups}


def encode(name):
    """A path component as Claude Code encodes it in a project directory name."""
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def claude_matcher(scope):
    """Function: project_dir -> group name or None."""
    roots = "|".join(re.escape(encode(r)) for r in scope["roots"])
    rules = [(g, re.compile(rf"-(?:{roots})-{re.escape(encode(repo))}{PROJECT_DIR_WORKTREE}"))
             for g, repos in scope["groups"].items() for repo in repos]

    def match(project_dir):
        return next((g for g, rx in rules if rx.search(project_dir or "")), None)
    return match


def gemini_matcher(scope):
    """Function: project (owner/repo or a folder) -> group name or None."""
    by_repo = {repo: g for g, repos in scope["groups"].items() for repo in repos}
    return lambda project: by_repo.get((project or "").rstrip("/").rsplit("/", 1)[-1])


def sid(source, session):
    return hashlib.sha256(f"{source}:{session}".encode()).hexdigest()[:16]


def claude_rows(records, scope):
    """Per-(session, model) rows from token_carbon history records."""
    match = claude_matcher(scope) if scope else (lambda _: "all")
    rows, days = {}, []
    for r in records:
        model = r["model"]
        if model.startswith("<") or not any(r.get(c, 0) for c in ("input", "cache_read",
                                                                  "cache_write", "output")):
            continue
        group = match(r.get("project_dir", "")) or OUT_OF_SCOPE
        row = rows.setdefault((sid("claude", r["session"]), model), {
            "sid": sid("claude", r["session"]), "source": "claude", "group": group,
            "model": model, "calls": 0, **{c: 0 for c in COMPONENTS}})
        row["calls"] += r.get("calls", 0)
        for c in COMPONENTS:
            row[c] += r.get(c, 0)
        days.append(r["day"])
    return list(rows.values()), days


def gemini_rows(records, scope):
    """Per-(conversation, model) rows from carbon_gemini.collect()'s records."""
    match = gemini_matcher(scope) if scope else (lambda _: "all")
    rows, days = {}, []
    for r in records.values():
        group = match(r.get("project")) or OUT_OF_SCOPE
        key = (sid("gemini", r["conversation"]), r["model"])
        row = rows.setdefault(key, {
            "sid": key[0], "source": "gemini", "group": group, "model": r["model"],
            "calls": 0, **{c: 0 for c in COMPONENTS}})
        row["calls"] += 1
        for c in ("input", "cache_read", "output"):
            row[c] += r.get(c, 0)
        days.append(r["day"])
    return list(rows.values()), days


def zero():
    return {"sessions": 0, "calls": 0, **{c: 0 for c in COMPONENTS}, "total": 0}


def add_row(t, row):
    t["calls"] += row["calls"]
    for c in COMPONENTS:
        t[c] += row[c]
    t["total"] += sum(row[c] for c in COMPONENTS)


def aggregate(rows):
    """{source: {groups: {group: {models, total}}, combined: {models, total}}}."""
    out = {}
    for source in sorted({r["source"] for r in rows}):
        mine = [r for r in rows if r["source"] == source]
        groups = {}
        for name in sorted({r["group"] for r in mine}):
            groups[name] = _block([r for r in mine if r["group"] == name])
        in_scope = [r for r in mine if r["group"] != OUT_OF_SCOPE]
        entry = {"recorded_components": RECORDED.get(source, list(COMPONENTS)),
                 "groups": {k: v for k, v in groups.items() if k != OUT_OF_SCOPE},
                 "combined": _block(in_scope)}
        if OUT_OF_SCOPE in groups:
            entry["out_of_scope"] = {"total": groups[OUT_OF_SCOPE]["total"]}
        out[source] = entry
    return out


def _block(rows):
    models, total = {}, zero()
    for model in sorted({r["model"] for r in rows}):
        sub = [r for r in rows if r["model"] == model]
        m = zero()
        m["sessions"] = len({r["sid"] for r in sub})
        for r in sub:
            add_row(m, r)
        models[model] = m
    total["sessions"] = len({r["sid"] for r in rows})
    for r in rows:
        add_row(total, r)
    return {"models": models, "total": total}


def build(rows, coverage, scope_configured, aggregate_only=False):
    doc = {"schema": SCHEMA, "scope_configured": scope_configured, "coverage": coverage,
           "counts": aggregate(rows)}
    if not aggregate_only:
        doc["sessions"] = sorted(rows, key=lambda r: (r["source"], r["sid"], r["model"]))
    return doc


def coverage_of(days_by_source):
    return {s: {"first_day": min(d), "last_day": max(d), "days_with_data": len(set(d))}
            for s, d in days_by_source.items() if d}


def export_counts(claude_records, gemini_records, scope, aggregate_only=False):
    """The export document for one machine."""
    c_rows, c_days = claude_rows(claude_records, scope)
    g_rows, g_days = gemini_rows(gemini_records, scope)
    cov = coverage_of({"claude": c_days, "gemini": g_days})
    return build(c_rows + g_rows, cov, scope is not None, aggregate_only)


def merge_exports(docs):
    """Combine exports of several machines into an aggregate-only document.

    A session seen on more than one machine keeps, per model and component,
    the larger count (a session's counts only grow), as in the ledger merges.
    Every input must carry its session rows.
    """
    if not docs:
        raise ValueError("nothing to merge")
    merged, cov = {}, {}
    configured = all(d.get("scope_configured") for d in docs)
    for d in docs:
        if d.get("schema") != SCHEMA:
            raise ValueError(f"unknown schema {d.get('schema')!r}")
        if "sessions" not in d:
            raise ValueError("an input has no session rows (exported with --aggregate-only)")
        for r in d["sessions"]:
            old = merged.setdefault((r["source"], r["sid"], r["model"]), dict(r))
            for c in ("calls",) + COMPONENTS:
                old[c] = max(old[c], r[c])
        for s, c in d.get("coverage", {}).items():
            have = cov.setdefault(s, {"first_day": c["first_day"], "last_day": c["last_day"]})
            have["first_day"] = min(have["first_day"], c["first_day"])
            have["last_day"] = max(have["last_day"], c["last_day"])
    out = build(list(merged.values()), cov, configured, aggregate_only=True)
    out["machines_merged"] = len(docs)
    return out


def write_json(doc, path):
    Path(path).write_text(json.dumps(doc, indent=1) + "\n")
