#!/usr/bin/env python3
"""Development carbon for one repository, from every source, as a markdown page.

Collects Claude Code (token_carbon's history), VS Code Copilot Chat
(carbon_vscode), Copilot's cloud agent and code review (carbon_copilot),
GitHub Actions CI (carbon_gha) and Slurm jobs (carbon_slurm); max-merges the
raw records into a JSON ledger so nothing is lost when transcripts, chat logs
or sacct records expire; costs them; attributes them to features through
PRs; and writes a markdown page fragment, a JSON summary and badge text.

Usage:
    carbon_report.py --config dev_carbon.toml --markdown page.md --summary out.json
    carbon_report.py --config dev_carbon.toml --offline     # ledger only, no fetching
    carbon_report.py --config dev_carbon.toml --badge       # print the badge text

The config is TOML (Python 3.11+, or with tomli installed) or JSON with the
same structure; see the README for the schema.
"""

import argparse
import copy
import fnmatch
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import carbon_common as cc
import carbon_copilot
import carbon_gha
import carbon_slurm
import carbon_vscode
import token_carbon as tc

SOURCES = ("claude", "vscode", "copilot", "gha", "slurm")
SOURCE_LABELS = {"claude": "Claude Code", "vscode_copilot": "VS Code Copilot Chat",
                 "copilot": "Copilot cloud agent and review", "gha": "GitHub Actions CI",
                 "slurm": "OzSTAR/NT Slurm jobs"}
HEADLINE_CLASSES = ("dev", "validation")
UNATTRIBUTED = "main / unattributed"
NO_FEATURE_BRANCHES = ("main", "master", "HEAD", "-", "?", "", "gh-pages")

# TokenClimate's plausible range for the cache-read energy factor (x input).
CACHE_READ_RANGE = (0.05, tc.CACHE_READ_SCALE, 0.20)


def load_config(path):
    path = Path(path).expanduser()
    if path.suffix == ".json":
        return json.loads(path.read_text())
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            sys.exit(f"{path}: reading TOML needs Python 3.11+ (or tomli); "
                     "give the same settings as a .json file instead")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def with_overrides(defaults, overrides):
    """A copy of a module PARAMS table with config overrides; lists become tuples."""
    p = copy.deepcopy(defaults)
    for k, v in (overrides or {}).items():
        p[k] = tuple(v) if isinstance(v, list) else v
    return p


def collect(cfg, ledger_path, only=SOURCES):
    """Fetch every enabled source and max-merge it into the ledger."""
    slug = cfg["repo"]
    known = cc.load_ledger(ledger_path)["sources"]
    fresh = {}

    def attempt(name, fn):
        if name != "prs" and (name not in only or not cfg.get(name, {}).get("enabled", True)):
            return
        try:
            fresh.update(fn())
        except Exception as e:  # one broken source should not stop the others
            cc.warn(f"{name}: {e!r}")

    attempt("vscode", lambda: carbon_vscode.collect(
        cfg.get("vscode", {}).get("folders", []),
        cfg.get("vscode", {}).get("storage", carbon_vscode.STORAGE)))
    attempt("copilot", lambda: carbon_copilot.collect(
        slug, cfg.get("github_user", slug.split("/")[0]), known))
    attempt("gha", lambda: {"gha": carbon_gha.collect(slug, known.get("gha"))})

    def slurm():
        s = cfg.get("slurm", {})
        return {"slurm": carbon_slurm.collect(
            s.get("local_dirs", []), s.get("host"), s.get("user"), s.get("remote_dirs", []),
            s.get("start", "2026-01-01"))}

    attempt("slurm", slurm)
    attempt("claude", lambda: {"claude": claude_records(cfg)})
    attempt("prs", lambda: {"prs": pr_list(slug)})
    return cc.update_ledger(ledger_path, fresh)


def claude_records(cfg):
    """token_carbon history records for the configured projects (refreshes the history)."""
    pats = cfg.get("claude", {}).get("projects", [])
    keep = {}
    for r in tc.update_history():
        if r["model"].startswith("<") or not any(r.get(c, 0) for c in tc.COUNTS):
            continue
        hay = " ".join((r.get("project", ""), r.get("project_dir", ""), r.get("cwd", "")))
        if any(p in hay for p in pats):
            keep[tc.record_key(r["session"], r["day"], r["model"], r.get("branch", "?"))] = {
                k: v for k, v in r.items() if k != "legacy"}
    return keep


def pr_list(slug):
    out = cc.run(["gh", "pr", "list", "-R", slug, "--state", "all", "--limit", "5000",
                  "--json", "number,title,headRefName,createdAt"])
    if out is None:
        return {}
    return {str(p["number"]): {"number": p["number"], "title": p["title"],
                               "branch": p["headRefName"], "created": p["createdAt"]}
            for p in json.loads(out)}


def sha_prs(slug, shas, known):
    """{sha: [PR numbers]} for pinned commits, from the ledger or the GitHub API."""
    out = {}
    for sha in sorted(s for s in shas if s):
        if sha in known:
            out[sha] = known[sha]
            continue
        prs = cc.gh_json(f"repos/{slug}/commits/{sha}/pulls", jq=".[].number", quiet=True)
        if prs is not None:
            out[sha] = {"prs": prs}
    return out


def session_classes(classes):
    """{session id: class} from each class's sessions_file (one id per line).

    A '#' starts a comment and blank lines are ignored. A missing file only
    warns. The ids are never written to any output.
    """
    out = {}
    for cls in sorted(classes, key=lambda c: (c != "science", c != "validation"), reverse=True):
        path = classes[cls].get("sessions_file")
        if not path:
            continue
        try:
            text = Path(path).expanduser().read_text()
        except OSError:
            print(f"warning: sessions_file for class '{cls}' not readable; ignored", file=sys.stderr)
            continue
        for line in text.splitlines():
            sid = line.split("#", 1)[0].strip()
            if sid:
                out[sid] = cls
    return out


def claude_items(records, classes):
    items = []
    listed = session_classes(classes)
    for key, r in records.items():
        family = tc.model_family(r["model"])
        tokens = {t: r.get(t, 0) for t in tc.TYPES}
        kwh, kg, by_type = [], [], {}
        for scale in CACHE_READ_RANGE:
            e = cc.llm_cost(family, tokens, cache_read_scale=scale)
            kwh.append(e[0])
            kg.append(e[1])
        for t in tc.TYPES:
            by_type[t] = cc.llm_cost(family, {t: tokens[t]})[1]
        items.append({
            "source": "claude", "id": key, "day": r["day"], "label": tc.FAMILIES[family]["label"],
            "kind": "model calls", "branch": r.get("branch"), "pr": None,
            "class": listed.get(r.get("session")) or cc.classify(
                classes, branch=r.get("branch"), dir=r.get("cwd")),
            "tokens": tokens, "kg_by_type": by_type, "calls": r.get("calls", 0),
            "kwh": kwh, "kg": kg,
        })
    return items


def cost_all(cfg, ledger):
    """Every costed item, plus the Copilot cross-check and calibration."""
    src = ledger["sources"]
    slug = cfg["repo"]
    classes = cfg.get("classes", {})
    params = cfg.get("params", {})
    items = claude_items(src.get("claude", {}), classes)
    vs = carbon_vscode.cost(src.get("vscode_copilot", {}),
                            params=with_overrides(carbon_vscode.PARAMS, params.get("vscode")))
    for i in vs:
        r = src["vscode_copilot"][i["id"]]
        i["class"] = max((cc.classify(classes, dir=f) for f in r.get("folders", [])),
                         key=lambda c: c == "science", default="dev")
    items += vs
    gha_params = with_overrides(carbon_gha.PARAMS, params.get("gha"))
    # Copilot runs are costed by carbon_copilot; a ledger from before the gha
    # collector skipped them may still hold them.
    copilot_runs = src.get("copilot_runs", {})
    items += [i for i in carbon_gha.cost(src.get("gha", {}), gha_params)
              if i["id"].startswith(slug + "#") and i["id"] not in copilot_runs]
    cop, check, cal = carbon_copilot.cost(
        src, slug, with_overrides(carbon_copilot.PARAMS, params.get("copilot")), gha_params)
    items += cop
    # CI and Copilot runs follow their branch's class, like Claude sessions.
    for i in items:
        if i["source"] in ("gha", "copilot"):
            i["class"] = cc.classify(classes, branch=i.get("branch"))
    s = cfg.get("slurm", {})
    items += carbon_slurm.cost(src.get("slurm", {}), classes, s.get("include", ["*"]),
                               s.get("exclude", []),
                               with_overrides(carbon_slurm.PARAMS, params.get("slurm")))
    since, until = cfg.get("since"), cfg.get("until")
    items = [i for i in items if (not since or i["day"] >= since) and (not until or i["day"] <= until)]
    return items, check, cal


def attribute(items, cfg, ledger, offline=False):
    """Set each item's feature: a config override, its PR's title, or main / unattributed."""
    slug = cfg["repo"]
    prs = ledger["sources"].get("prs", {})
    by_branch = {}
    for p in sorted(prs.values(), key=lambda p: p["created"]):
        by_branch[p["branch"]] = p
    known = ledger["sources"].get("sha_prs", {})
    shas = {i.get("sha") for i in items if i.get("sha") and not i.get("pr")}
    found = known if offline else sha_prs(slug, shas, known)
    if not offline and found:
        ledger = cc.update_ledger(cfg["_ledger"], {"sha_prs": found})
    overrides = cfg.get("features", {})
    for i in items:
        label = None
        for pattern, value in overrides.items():
            target = i["label"] if pattern.startswith("job:") else i.get("branch") or ""
            if fnmatch.fnmatch(target, pattern[4:] if pattern.startswith("job:") else pattern):
                label = value
                break
        pr = i.get("pr")
        if pr is None and i.get("branch") and i["branch"] not in NO_FEATURE_BRANCHES:
            p = by_branch.get(i["branch"])
            pr = p["number"] if p else None
        if pr is None and i.get("sha") in found and found[i["sha"]].get("prs"):
            pr = min(found[i["sha"]]["prs"])
        i["pr"] = pr
        i["override"] = label is not None
        # Work for a PR from an excluded branch (e.g. CI on main after its
        # merge) takes that branch's class.
        p = prs.get(str(pr)) if pr is not None else None
        if p and i["class"] in HEADLINE_CLASSES:
            branch_class = cc.classify(cfg.get("classes", {}), branch=p["branch"])
            if branch_class not in HEADLINE_CLASSES:
                i["class"] = branch_class
        if label is None:
            p = prs.get(str(pr)) if pr is not None else None
            label = f"#{pr} {p['title']}" if p else f"#{pr}" if pr is not None else UNATTRIBUTED
        i["feature"] = label
    return items


# Days after which a source's raw records are gone, or about to be: Claude
# Code deletes transcripts after 30 days, NT's sacct keeps about six months,
# and GitHub keeps Actions runs and usage reports for about 90 days. A VS Code
# chat record expires when its session file is deleted. Config:
# [retention_days].
RETENTION_DAYS = {"claude": 25, "slurm": 150, "gha": 85, "copilot": 85}
ARCHIVE_VERSION = 1
EXCLUDED_LABEL = "excluded"


def hide_excluded(items):
    """Drop the names (labels and features) of items outside the headline."""
    for i in items:
        if i["class"] not in HEADLINE_CLASSES:
            i["label"] = EXCLUDED_LABEL
            i["feature"] = ""
            i["pr"] = None
            i["override"] = True


def expired(i, ledger, cfg, today):
    """Whether an item's raw record has gone, or is about to, from its source."""
    if i["source"] == "vscode_copilot":
        r = ledger["sources"].get("vscode_copilot", {}).get(i["id"], {})
        storage = Path(cfg.get("vscode", {}).get("storage", carbon_vscode.STORAGE))
        session = (storage.expanduser() / "workspaceStorage" / r.get("workspace", "?")
                   / "chatSessions" / f"{r.get('session', '?')}.jsonl")
        return not session.exists()
    days = {**RETENTION_DAYS, **cfg.get("retention_days", {})}.get(i["source"])
    if days is None or not i["day"]:
        return False
    return i["day"] < (today - timedelta(days=days)).isoformat()


def archive_feature(i):
    """An archive row's feature: an override label or '#N', never a PR title."""
    if i.get("override") or i.get("pr") is None:
        return i.get("feature", UNATTRIBUTED)
    return f"#{i['pr']}"


def compact(items):
    """Archive rows: items summed by day, source, label, kind, class and feature."""
    rows = {}
    for i in items:
        feature = archive_feature(i)
        key = "|".join((i["day"], i["source"], i["label"], i["kind"], i["class"], feature))
        r = rows.setdefault(key, {
            "day": i["day"], "source": i["source"], "label": i["label"], "kind": i["kind"],
            "class": i["class"], "feature": feature, "pr": i.get("pr"),
            "override": bool(i.get("override")), "n": 0, "calls": 0, "tokens": {},
            "kg_by_type": {}, "kwh": [0.0] * 3, "kg": [0.0] * 3})
        r["n"] += 1
        r["calls"] += i.get("calls", 0)
        r["kwh"] = add(r["kwh"], i["kwh"])
        r["kg"] = add(r["kg"], i["kg"])
        for f in ("tokens", "kg_by_type"):
            for t, v in (i.get(f) or {}).items():
                r[f][t] = r[f].get(t, 0) + v
    return rows


def merge_archive(old, new):
    """Max-merge archive rows into old, field by field, so totals never drop."""
    for key, r in new.items():
        prev = old.get(key)
        if prev is None:
            old[key] = r
            continue
        for f, v in r.items():
            if isinstance(v, list):
                prev[f] = [max(a, b) for a, b in zip(prev.get(f, v), v)]
            elif isinstance(v, dict):
                d = prev.setdefault(f, {})
                for t, x in v.items():
                    d[t] = max(d.get(t, x), x)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                prev[f] = max(prev.get(f, v), v)
    return old


def with_archive(items, cfg, ledger, path, today=None):
    """Archive expired items into path; return the live items plus the archive.

    A (source, day) with any expired item is archived whole, and from then on
    that source's day is taken only from the archive, so nothing is counted
    twice. Archive rows keep no PR titles; they are looked up again here.
    """
    today = today or date.today()
    path = Path(path)
    archive = (json.loads(path.read_text()) if path.exists()
               else {"version": ARCHIVE_VERSION, "rows": {}})
    gone = {(i["source"], i["day"]) for i in items if expired(i, ledger, cfg, today)}
    merge_archive(archive["rows"],
                  compact([i for i in items if (i["source"], i["day"]) in gone]))
    archive["rows"] = dict(sorted(archive["rows"].items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(archive, indent=0, sort_keys=True) + "\n")
    days = {(r["source"], r["day"]) for r in archive["rows"].values()}
    out = [i for i in items if (i["source"], i["day"]) not in days]
    prs = ledger["sources"].get("prs", {})
    since, until = cfg.get("since"), cfg.get("until")
    for key, r in archive["rows"].items():
        if (since and r["day"] < since) or (until and r["day"] > until):
            continue
        feature = r["feature"]
        if not r.get("override") and str(r.get("pr")) in prs:
            feature = f"#{r['pr']} {prs[str(r['pr'])]['title']}"
        out.append({**r, "id": "archive:" + key, "feature": feature, "branch": None,
                    "archived": True})
    return out


def add(a, b):
    return [x + y for x, y in zip(a, b)]


def group(items, key):
    out = defaultdict(lambda: {"n": 0, "kwh": [0.0] * 3, "kg": [0.0] * 3})
    for i in items:
        g = out[key(i)]
        g["n"] += 1
        g["kwh"] = add(g["kwh"], i["kwh"])
        g["kg"] = add(g["kg"], i["kg"])
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["kg"][1]))


def summarize(items, check, cal, cfg):
    head = [i for i in items if i["class"] in HEADLINE_CLASSES]
    total = group(head, lambda i: "total").get("total", {"n": 0, "kwh": [0.0] * 3, "kg": [0.0] * 3})
    claude = [i for i in head if i["source"] == "claude"]
    by_type = {t: {"tokens": sum(i["tokens"].get(t, 0) for i in claude),
                   "kg": sum(i["kg_by_type"].get(t, 0) for i in claude)} for t in tc.TYPES}
    return {
        "repo": cfg["repo"],
        "generated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "archived": group([i for i in items if i.get("archived")], lambda i: i["source"]),
        "first_day": min((i["day"] for i in head if i["day"]), default=None),
        "last_day": max((i["day"] for i in head if i["day"]), default=None),
        "total": total,
        "by_source": group(head, lambda i: i["source"]),
        "by_class": group(items, lambda i: i["class"]),
        "by_kind": group(head, lambda i: f"{SOURCE_LABELS.get(i['source'], i['source'])}|{i['kind']}"),
        "by_label": group(head, lambda i: f"{i['source']}|{i['label']}"),
        "by_feature": group(head, lambda i: i.get("feature", UNATTRIBUTED)),
        "science": group([i for i in items if i["class"] == "science"],
                         lambda i: f"{SOURCE_LABELS.get(i['source'], i['source'])}|{i['label']}"),
        "claude_token_types": by_type,
        "copilot_check": check,
        "copilot_calibration": cal,
        "prs": {i["feature"]: i["pr"] for i in head if i.get("pr")},
    }


def fmt(x):
    """Three significant figures, no exponent, thousands separators."""
    if x == 0:
        return "0"
    if abs(x) < 0.001:
        return "<0.001"
    if abs(x) >= 100:
        return f"{x:,.0f}"
    return f"{x:.3g}"


def rng(t):
    return f"{fmt(t[0])}–{fmt(t[2])}"


def badge_text(summary):
    kg = summary["total"]["kg"][1]
    return f"dev carbon | {fmt(kg)} kg CO2e"


def badge_url(summary, color="2e7d32"):
    kg = fmt(summary["total"]["kg"][1])
    return ("https://img.shields.io/badge/" + quote("dev carbon".replace("-", "--")) + "-"
            + quote(f"{kg} kg CO₂e".replace("-", "--")) + f"-{color}")


def table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---:" if h.startswith(("kWh", "kg", "Items", "Tokens", "Runs", "Min"))
                          else "---" for h in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def markdown(summary, cfg, top=15):
    s = summary
    slug = cfg["repo"]
    t = s["total"]
    lines = [
        f"Estimated development carbon for `{slug}` from {s['first_day']} to {s['last_day']}: "
        f"**{fmt(t['kg'][1])} kg CO₂e** (range {rng(t['kg'])} kg), from "
        f"{fmt(t['kwh'][1])} kWh (range {rng(t['kwh'])} kWh). Data analysis runs are listed "
        "separately below and are not in this total.",
        "",
        "## By source",
        "",
        table(["Source", "Items", "kWh", "kWh range", "kg CO₂e", "kg range"],
              [[SOURCE_LABELS.get(k, k), g["n"], fmt(g["kwh"][1]), rng(g["kwh"]),
                fmt(g["kg"][1]), rng(g["kg"])] for k, g in s["by_source"].items()]),
        "",
        "## By call and token type",
        "",
        table(["Source", "Type", "Items", "kWh", "kg CO₂e", "kg range"],
              [[*k.split("|", 1), g["n"], fmt(g["kwh"][1]), fmt(g["kg"][1]), rng(g["kg"])]
               for k, g in s["by_kind"].items()]),
        "",
    ]
    tt = s["claude_token_types"]
    if any(v["tokens"] for v in tt.values()):
        lines += ["Claude Code by token type (mid estimate):", "",
                  table(["Token type", "Tokens", "kg CO₂e"],
                        [[k.replace("_", " "), f"{v['tokens']:,}", fmt(v["kg"])] for k, v in tt.items()]),
                  ""]
    models = [(k.split("|", 1), g) for k, g in s["by_label"].items()
              if k.split("|", 1)[0] in ("claude", "vscode_copilot", "gha")]
    if models:
        lines += ["By model or workflow:", "",
                  table(["Source", "Model or workflow", "Items", "kg CO₂e", "kg range"],
                        [[SOURCE_LABELS.get(src, src), lab, g["n"], fmt(g["kg"][1]), rng(g["kg"])]
                         for (src, lab), g in models]), ""]
    feats = list(s["by_feature"].items())
    lines += [f"## By feature (top {min(top, len(feats))} of {len(feats)})", "",
              "Each item is attributed to a pull request through its branch, the commit a job "
              "pinned, or the PR a Copilot run served, and labelled with the PR title.", "",
              table(["Feature", "Items", "kg CO₂e", "kg range"],
                    [[feature_link(k, s["prs"].get(k), slug), g["n"], fmt(g["kg"][1]), rng(g["kg"])]
                     for k, g in feats[:top]]), ""]
    if s["science"]:
        lines += ["## Data analysis runs (not in the total)", "",
                  table(["Source", "Job or session", "Items", "kWh", "kg CO₂e"],
                        [[*k.split("|", 1), g["n"], fmt(g["kwh"][1]), fmt(g["kg"][1])]
                         for k, g in s["science"].items()]), ""]
    if s["copilot_check"]:
        lines += ["## Copilot cross-check", "",
                  "Run time times an assumed token rate, against billing (AI Credits or premium "
                  "requests) attributed to this repository. The headline uses billing where a "
                  "month has it.", "",
                  table(["Month", "Kind", "Runs", "Minutes", "kg (run time)", "kg (billing)", "Billing basis"],
                        [[c["month"], c["kind"], c["runs"], fmt(c["minutes"]), rng(c["runtime_kg"]),
                          rng(c["billing_kg"]) if c["billing_kg"] else "–", c["billing_note"] or "–"]
                         for c in s["copilot_check"]]), ""]
    lines += methodology(s, cfg)
    return "\n".join(lines) + "\n"


def feature_link(label, pr, slug):
    if pr and label.startswith(f"#{pr}"):
        return f"[#{pr}](https://github.com/{slug}/pull/{pr}){label[len(str(pr)) + 1:]}"
    return label


def methodology(s, cfg):
    sl = with_overrides(carbon_slurm.PARAMS, cfg.get("params", {}).get("slurm"))
    gh = with_overrides(carbon_gha.PARAMS, cfg.get("params", {}).get("gha"))
    cal = s["copilot_calibration"]
    return [
        "## Methodology", "",
        "Every figure is an estimate with a low, mid and high value; tables show the mid value "
        "and the range. Energy is facility energy (server energy times the data centre's PUE).", "",
        table(["Source", "Telemetry", "Energy model", "Main assumptions"], [
            ["Claude Code", "Token counts by type from local transcripts",
             "TokenClimate per-token factors by model family",
             f"Cache reads at {CACHE_READ_RANGE[1]}× input energy (range "
             f"{CACHE_READ_RANGE[0]}–{CACHE_READ_RANGE[2]}); PUE {tc.PUE}; "
             f"{tc.CO2_G_PER_WH:.3f} g CO₂e per server Wh"],
            ["VS Code Copilot Chat", "Prompt and output tokens per request from local chat logs",
             "TokenClimate factors for an assumed model class",
             "GPT and other non-Claude models placed in a Claude size class by assumption; "
             "prompts costed as uncached input; the high value counts every model call's prompt; "
             "requests without counts take the median of counted ones"],
            ["Copilot cloud agent and review", "Workflow run time; billed AI Credits and premium requests",
             "Tokens from credits (or run time) on TokenClimate Sonnet factors",
             f"{fmt(cal['credits_per_mtok'])} credits per million tokens, measured on local chat "
             f"({'measured' if cal['measured'] else 'fallback'}); account-wide billing attributed "
             "by this repository's share of local use or agent PRs"],
            ["GitHub Actions CI", "Runner time per job", "Green Algorithms",
             f"4 vCPU at {carbon_gha.RUNNERS['ubuntu']['p_core_w']:.2f} W each, 16 GB, usage "
             f"{gh['u_cpu'][0]}–{gh['u_cpu'][2]}, PUE {gh['pue']}, {gh['grid_kg_per_kwh']} kg/kWh"],
            ["OzSTAR/NT Slurm jobs", "sacct allocation and run time; NT Job Report usage",
             "Green Algorithms",
             f"EPYC 7543 at {sl['p_core_w']:.2f} W per core, A100 at {sl['p_gpu_w']:.0f} W, measured "
             f"usage where reported, PUE {sl['pue']}, Victorian grid {sl['grid_kg_per_kwh']} kg/kWh"],
        ]), "",
        "Not included: Claude sessions run in the cloud or on other machines, the energy to "
        "train the models, and the embodied carbon of local hardware. The cache-read factor "
        "dominates the uncertainty of the Claude figure, and Copilot's cloud inference is "
        "inferred from billing or run time rather than measured.", "",
        "### Sources", "",
        "- TokenClimate, methodology `tokenclimate-v3-2026-09`, https://tokenclimate.com/en/methodology",
        "- Lannelongue, L., Grealey, J. & Inouye, M. (2021), Green Algorithms: Quantifying the "
        "carbon footprint of computation, *Advanced Science* 8, 2100707, "
        "https://doi.org/10.1002/advs.202100707",
        "- DCCEEW (2026), *Australian National Greenhouse Accounts Factors*, Table 1, "
        "https://www.dcceew.gov.au/climate-change/publications/national-greenhouse-accounts-factors",
        "- US EPA, eGRID2022 (US average grid intensity, for GitHub-hosted runners)",
        "",
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, help="TOML or JSON config")
    ap.add_argument("--ledger", help="ledger path (default: the config's, else "
                                     "~/.claude/carbon/ledger-<repo>.json)")
    ap.add_argument("--offline", action="store_true", help="cost the ledger without fetching")
    ap.add_argument("--sources", default=",".join(SOURCES),
                    help=f"comma-separated subset of {','.join(SOURCES)} to fetch")
    ap.add_argument("--markdown", metavar="PATH", help="write the page fragment")
    ap.add_argument("--summary", metavar="PATH", help="write the JSON summary")
    ap.add_argument("--badge", action="store_true", help="print the badge text and URL")
    ap.add_argument("--archive", metavar="PATH",
                    help="compact expired records into this archive (max-merged) and "
                         "report the archive plus live records")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    ledger_path = Path(args.ledger or cfg.get("ledger") or
                       tc.DATA_DIR / f"ledger-{cfg['repo'].split('/')[1]}.json").expanduser()
    cfg["_ledger"] = ledger_path
    if args.offline:
        ledger = cc.load_ledger(ledger_path)
    else:
        ledger = collect(cfg, ledger_path, tuple(args.sources.split(",")))
    items, check, cal = cost_all(cfg, ledger)
    attribute(items, cfg, ledger, args.offline)
    if cfg.get("hide_excluded"):
        hide_excluded(items)
    if args.archive:
        items = with_archive(items, cfg, ledger, args.archive)
    summary = summarize(items, check, cal, cfg)
    summary["badge"] = {"text": badge_text(summary), "url": badge_url(summary)}
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=1) + "\n")
    if args.markdown:
        Path(args.markdown).write_text(markdown(summary, cfg))
    if args.badge:
        print(summary["badge"]["text"])
        print(summary["badge"]["url"])
    if not (args.summary or args.markdown or args.badge):
        print(markdown(summary, cfg))


if __name__ == "__main__":
    main()
