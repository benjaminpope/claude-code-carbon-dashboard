#!/usr/bin/env python3
"""Tally Claude Code token usage by type and estimate energy and CO2e.

Reads the local transcripts in ~/.claude/projects (including subagent
transcripts), deduplicates API calls by message id, and splits usage into
uncached input, cache writes, cache reads and output.

Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default), so
every run also folds the totals into a history file (history.json in the data
directory, ~/.claude/carbon by default). Totals are kept per session, day and
model; a record whose transcript has been deleted keeps its last value. All
reports read from that history, so they keep covering days whose transcripts
are gone. The history holds token counts only, never conversation text.

Emission factors come from TokenClimate (tokenclimate-v3-2026-09, Claude
Opus): 238 Wh and 90 g CO2e per million input tokens, 5.1 kWh and 1.9 kg
CO2e per million generated tokens. TokenClimate gives no factors for cache
traffic, so cache writes and cache reads are scaled from the input factor by
CACHE_WRITE_SCALE and CACHE_READ_SCALE below. Those two numbers are
assumptions, not measurements; change them if you have better data.

Usage:
    token_carbon.py                        # all history, all projects
    token_carbon.py --since 2026-09-01     # only days on or after this date
    token_carbon.py --project myproject    # substring match on project name
    token_carbon.py --by project           # also: session, model, day
    token_carbon.py --json                 # machine-readable output
    token_carbon.py --html dashboard.html  # self-contained dashboard page
"""

import argparse
import fcntl
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_CARBON_HOME", Path.home() / ".claude" / "carbon"))
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))
TEMPLATE = Path(__file__).resolve().parent / "dashboard_template.html"
HISTORY_VERSION = 1

# Per million tokens, TokenClimate tokenclimate-v3-2026-09 (Claude Opus).
INPUT_WH, INPUT_G = 238.0, 90.0
OUTPUT_WH, OUTPUT_G = 5100.0, 1900.0

# Assumptions relative to uncached input. A cache write does the same prefill
# compute as input plus a KV-cache store; a cache read skips the compute and
# mostly moves memory. 0.1 mirrors the price ratio and is only a proxy.
CACHE_WRITE_SCALE = 1.0
CACHE_READ_SCALE = 0.1

TYPES = ("input", "cache_write", "cache_read", "output")
COUNTS = TYPES + ("calls",)
FACTORS = {  # (Wh, g CO2e) per million tokens
    "input": (INPUT_WH, INPUT_G),
    "cache_write": (INPUT_WH * CACHE_WRITE_SCALE, INPUT_G * CACHE_WRITE_SCALE),
    "cache_read": (INPUT_WH * CACHE_READ_SCALE, INPUT_G * CACHE_READ_SCALE),
    "output": (OUTPUT_WH, OUTPUT_G),
}

# Everyday equivalents, kg CO2e per unit, from the ALPLA CO2 Comparison Tool
# (https://www.alpla.com/en/sustainability/co2-comparison-tool, read 2026-10-01;
# factors taken from the calculator's own script). Packaging is produced in
# Germany. ALPLA is a plastic packaging maker; the figures are theirs.
COMPARISONS = [
    {"kg": 0.0002, "one": "Google search", "many": "Google searches"},
    {"kg": 0.00432, "one": "message sent to ChatGPT", "many": "messages sent to ChatGPT"},
    {"kg": 0.0048, "one": "PET bottle cap", "many": "PET bottle caps"},
    {"kg": 0.00632, "one": "1 l reusable PET bottle", "many": "1 l reusable PET bottles"},
    {"kg": 0.0084, "one": "1 l recycled-PET bottle", "many": "1 l recycled-PET bottles"},
    {"kg": 0.01308, "one": "1 l reusable glass bottle", "many": "1 l reusable glass bottles"},
    {"kg": 0.038, "one": "250 ml HDPE bottle", "many": "250 ml HDPE bottles"},
    {"kg": 0.055, "one": "hour of video streaming", "many": "hours of video streaming"},
    {"kg": 0.065, "one": "1 l PET bottle", "many": "1 l PET bottles"},
    {"kg": 0.089, "one": "500 ml aluminium can", "many": "500 ml aluminium cans"},
    {"kg": 0.165, "one": "detergent bottle", "many": "detergent bottles"},
    {"kg": 0.4, "one": "coffee cup", "many": "coffee cups"},
    {"kg": 0.488, "one": "portion of spaghetti with tomato sauce",
     "many": "portions of spaghetti with tomato sauce"},
    {"kg": 4.0, "one": "hamburger with fries", "many": "hamburgers with fries"},
    {"kg": 24.62, "one": "tree's annual CO\u2082 uptake", "many": "trees' annual CO\u2082 uptake"},
    {"kg": 80.0, "one": "manufactured smartphone", "many": "manufactured smartphones"},
    {"kg": 232.0, "one": "economy flight Zurich\u2013London",
     "many": "economy flights Zurich\u2013London"},
    {"kg": 4600.0, "one": "year of driving an average car",
     "many": "years of driving an average car"},
    {"kg": 7250.0, "one": "year of an average European's emissions",
     "many": "years of an average European's emissions"},
    {"kg": 13800.0, "one": "year of an average American's emissions",
     "many": "years of an average American's emissions"},
]


def closest_comparison(kg):
    """The everyday equivalent nearest to kg on a log scale, as (count, label)."""
    if kg <= 0:
        return 0, COMPARISONS[0]["many"]
    best = min(COMPARISONS, key=lambda c: abs(math.log(kg / c["kg"])))
    count = kg / best["kg"]
    shown = round(count, 1) if count < 10 else round(count)
    return count, best["one"] if shown == 1 else best["many"]


def format_comparison(kg):
    count, label = closest_comparison(kg)
    number = f"{count:.1f}".removesuffix(".0") if count < 10 else f"{round(count):,}"
    return f"{number} {label}"


def project_name(project, cwds):
    """Readable name for a ~/.claude/projects directory, e.g. 'myproject'."""
    for cwd in cwds:
        if re.sub(r"[^A-Za-z0-9]", "-", cwd) == project:
            return Path(cwd).name
    return "scratch" if "-scratch-" in project else project


def scan_transcripts(projects=PROJECTS):
    """Totals from the transcripts on disk, keyed by session|day|model."""
    records, seen, cwds = {}, set(), defaultdict(set)
    for path in sorted(projects.rglob("*.jsonl")):
        project = path.relative_to(projects).parts[0]
        calls = {}
        with open(path, errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                if rec.get("cwd"):
                    cwds[project].add(rec["cwd"])
                # Streaming writes one line per content block with the same
                # id; the last one carries the final output count.
                calls[msg.get("id") or rec.get("requestId")] = (rec, msg, usage)
        for mid, (rec, msg, usage) in calls.items():
            if mid in seen:
                continue
            seen.add(mid)
            session = rec.get("sessionId", path.stem)
            day = rec.get("timestamp", "")[:10]
            model = msg.get("model", "unknown")
            r = records.setdefault(f"{session}|{day}|{model}", {
                "session": session, "day": day, "model": model,
                "project_dir": project, **{c: 0 for c in COUNTS},
            })
            r["calls"] += 1
            r["input"] += usage.get("input_tokens", 0) or 0
            r["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
            r["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            r["output"] += usage.get("output_tokens", 0) or 0
    for r in records.values():
        r["project"] = project_name(r["project_dir"], sorted(cwds[r["project_dir"]]))
    return records


def merge(history, current):
    """Fold current transcript totals into the history, in place.

    A transcript only grows until it is deleted, so each count keeps the
    larger of its stored and current values. Records with no transcript left
    keep what was stored.
    """
    for key, cur in current.items():
        old = history.get(key)
        if old is None:
            history[key] = cur
            continue
        for c in COUNTS:
            old[c] = max(old.get(c, 0), cur[c])
        old["project"] = cur["project"]
    return history


def update_history(data_dir=DATA_DIR, projects=PROJECTS):
    """Scan transcripts, merge into history.json and return all records."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "history.json"
    # Several sessions can finish a turn at once; serialise the read-merge-write.
    with open(data_dir / "history.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = {}
        if path.exists():
            stored = json.loads(path.read_text())
            if stored.get("version") != HISTORY_VERSION:
                sys.exit(f"{path}: unknown history version {stored.get('version')}")
            history = stored["records"]
        merge(history, scan_transcripts(projects))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": HISTORY_VERSION, "records": history}, indent=1))
        tmp.replace(path)
    return list(history.values())


def select(records, project=None, since=None):
    return [r for r in records
            if (not project or project in r["project"] or project in r["project_dir"])
            and (not since or r["day"] >= since)]


def tally(records, by=None):
    groups = defaultdict(lambda: {c: 0 for c in COUNTS})
    for r in records:
        g = groups[r[by] if by else "total"]
        for c in COUNTS:
            g[c] += r[c]
    out = {}
    for name, g in groups.items():
        row = {"calls": g["calls"], "tokens": {}, "wh": {}, "g_co2e": {}}
        for t in TYPES:
            wh, gco2 = FACTORS[t]
            row["tokens"][t] = g[t]
            row["wh"][t] = g[t] / 1e6 * wh
            row["g_co2e"][t] = g[t] / 1e6 * gco2
        row["wh"]["total"] = sum(row["wh"][t] for t in TYPES)
        row["g_co2e"]["total"] = sum(row["g_co2e"][t] for t in TYPES)
        out[name] = row
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["g_co2e"]["total"]))


def write_html(records, out_path):
    """Render the dashboard: day x project rows, factors applied in-page."""
    rows = defaultdict(lambda: {c: 0 for c in COUNTS})
    for r in records:
        row = rows[(r["day"], r["project"])]
        for c in COUNTS:
            row[c] += r[c]
    data = {
        "generated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "factors": {t: {"wh": INPUT_WH if t != "output" else OUTPUT_WH,
                        "g": INPUT_G if t != "output" else OUTPUT_G} for t in TYPES},
        "scales": {"cache_write": CACHE_WRITE_SCALE, "cache_read": CACHE_READ_SCALE},
        "comparisons": COMPARISONS,
        "rows": [{"day": d, "project": p, **r} for (d, p), r in sorted(rows.items())],
    }
    html = TEMPLATE.read_text().replace(
        "/*DATA*/null", json.dumps(data).replace("</", "<\\/"))
    out_path = Path(out_path)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(html)
    tmp.replace(out_path)


def print_table(result):
    for name, row in result.items():
        print(f"\n== {name}  ({row['calls']:,} API calls)")
        print(f"{'type':<12}{'tokens':>16}{'Wh':>12}{'g CO2e':>12}{'share':>8}")
        total = row["g_co2e"]["total"] or 1
        for t in TYPES:
            print(f"{t:<12}{row['tokens'][t]:>16,}{row['wh'][t]:>12.1f}"
                  f"{row['g_co2e'][t]:>12.1f}{row['g_co2e'][t] / total:>8.0%}")
        print(f"{'total':<12}{sum(row['tokens'].values()):>16,}"
              f"{row['wh']['total']:>12.1f}{row['g_co2e']['total']:>12.1f}")
        print(f"\u2248 {format_comparison(row['g_co2e']['total'] / 1000)}")
    print(f"\nCache scales vs input: write x{CACHE_WRITE_SCALE}, read x{CACHE_READ_SCALE} "
          "(assumptions). Opus factors applied to every model.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="ISO date, e.g. 2026-09-01")
    ap.add_argument("--project", help="substring of the project name")
    ap.add_argument("--by", choices=["project", "session", "model", "day"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH", help="write the dashboard to PATH")
    args = ap.parse_args()
    records = select(update_history(), args.project, args.since)
    if args.html:
        write_html(records, args.html)
    elif args.json:
        json.dump({"factors_per_million": FACTORS, "groups": tally(records, args.by)},
                  sys.stdout, indent=2)
        print()
    else:
        print_table(tally(records, args.by))


if __name__ == "__main__":
    main()
