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

Energy and emission factors follow the TokenClimate methodology
(tokenclimate-v3-2026-09, https://tokenclimate.com/en/methodology): per-family
server energy for input and output tokens (Haiku, Sonnet, Opus, Fable), cache
writes at the input rate, cache reads at 0.08 times it, and CO2e = energy x
(PUE x grid intensity + embodied hardware carbon). Models that match no family
are counted as Opus and reported as such.

Usage:
    token_carbon.py                        # all history, all projects
    token_carbon.py --since 2026-09-01     # only days on or after this date
    token_carbon.py --project myproject    # substring match on project name
    token_carbon.py --by project           # also: session, model, family, day
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
from datetime import date, datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_CARBON_HOME", Path.home() / ".claude" / "carbon"))
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))
TEMPLATE = Path(__file__).resolve().parent / "dashboard_template.html"
HISTORY_VERSION = 1

# Server (IT) energy in Wh per million tokens, TokenClimate tokenclimate-v3-2026-09
# methodology, "Anthropic parameters". Only Sonnet is fitted to a published
# estimate; Opus is 2x Sonnet, Haiku 0.5x and Fable 2x Opus, hence the
# confidence levels.
FAMILIES = {
    "haiku": {"label": "Claude Haiku", "in_wh": 61, "out_wh": 1262, "confidence": "medium"},
    "sonnet": {"label": "Claude Sonnet", "in_wh": 119, "out_wh": 2525, "confidence": "high"},
    "opus": {"label": "Claude Opus", "in_wh": 238, "out_wh": 5050, "confidence": "medium"},
    "fable": {"label": "Claude Fable", "in_wh": 476, "out_wh": 10100, "confidence": "low"},
}
FALLBACK_FAMILY = "opus"

# CO2e per Wh of server energy: datacentre overhead (PUE) times grid carbon
# intensity, plus amortised hardware (embodied) carbon. AWS parameters from
# TokenClimate: PUE 1.14, 0.287 kg/kWh, 49 g/kWh.
PUE, GRID_G_PER_WH, EMBODIED_G_PER_WH = 1.14, 0.287, 0.049
CO2_G_PER_WH = PUE * GRID_G_PER_WH + EMBODIED_G_PER_WH  # 0.37618

# Relative to uncached input. A cache write runs the same prefill as input; a
# cache read reuses stored keys and values and costs 0.08x (TokenClimate,
# "Cache energy"; plausible range 0.05 to 0.20).
CACHE_WRITE_SCALE = 1.0
CACHE_READ_SCALE = 0.08

TYPES = ("input", "cache_write", "cache_read", "output")
COUNTS = TYPES + ("calls",)


def model_family(model):
    """'claude-sonnet-5-5' -> 'sonnet'. Unrecognised models count as Opus."""
    m = model.lower()
    for family in FAMILIES:
        if family in m:
            return family
    return "fable" if "mythos" in m else FALLBACK_FAMILY


def wh_per_mtok(family, token_type):
    f = FAMILIES[family]
    return {"input": f["in_wh"], "cache_write": f["in_wh"] * CACHE_WRITE_SCALE,
            "cache_read": f["in_wh"] * CACHE_READ_SCALE, "output": f["out_wh"]}[token_type]


def is_known_family(model):
    m = model.lower()
    return any(f in m for f in FAMILIES) or "mythos" in m


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
    {"kg": 4.0, "one": "burger with chips", "many": "burgers with chips"},
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


def pick_comparison(kg, day=None):
    """An everyday equivalent for kg, as (count, item).

    Without a day, the item nearest kg on a log scale. With a day (an ISO
    date), the nearest item and its two neighbours in the ranking take turns
    from one day to the next (nearest, one below, one above), so a given day
    always gets the same item and consecutive days differ. The dashboard
    uses the same rule.
    """
    i = min(range(len(COMPARISONS)),
            key=lambda j: abs(math.log(kg / COMPARISONS[j]["kg"])))
    if day is not None:
        choices = [j for j in (i, i - 1, i + 1) if 0 <= j < len(COMPARISONS)]
        epoch_day = date.fromisoformat(day).toordinal() - date(1970, 1, 1).toordinal()
        i = choices[epoch_day % len(choices)]
    return kg / COMPARISONS[i]["kg"], COMPARISONS[i]


def format_count(n):
    """Two significant figures below 1, one decimal below 10, else whole."""
    if n < 1:
        return f"{n:.2g}"
    if n < 10:
        return f"{n:.1f}".removesuffix(".0")
    return f"{round(n):,}"


def format_comparison(kg, day=None):
    if kg <= 0:
        return "nothing yet"
    count, item = pick_comparison(kg, day)
    number = format_count(count)
    return f"{number} {item['one'] if number == '1' else item['many']}"


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
                # Claude Code writes "<synthetic>" placeholder messages locally;
                # they are not API calls.
                if not usage or str(msg.get("model", "")).startswith("<"):
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
            if not r["model"].startswith("<")
            and (not project or project in r["project"] or project in r["project_dir"])
            and (not since or r["day"] >= since)]


def tally(records, by=None):
    zero = lambda: {t: 0.0 for t in TYPES}
    groups = defaultdict(lambda: {"calls": 0, "tokens": zero(), "wh": zero(), "g_co2e": zero()})
    for r in records:
        family = model_family(r["model"])
        g = groups[FAMILIES[family]["label"] if by == "family" else r[by] if by else "total"]
        g["calls"] += r["calls"]
        for t in TYPES:
            wh = r[t] / 1e6 * wh_per_mtok(family, t)
            g["tokens"][t] += r[t]
            g["wh"][t] += wh
            g["g_co2e"][t] += wh * CO2_G_PER_WH
    for g in groups.values():
        g["tokens"] = {t: int(v) for t, v in g["tokens"].items()}
        g["wh"]["total"] = sum(g["wh"][t] for t in TYPES)
        g["g_co2e"]["total"] = sum(g["g_co2e"][t] for t in TYPES)
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]["g_co2e"]["total"]))


def factor_table():
    """Wh and g CO2e per million tokens, by family and token type."""
    return {f: {t: {"wh": wh_per_mtok(f, t), "g_co2e": wh_per_mtok(f, t) * CO2_G_PER_WH}
                for t in TYPES} for f in FAMILIES}


def write_html(records, out_path):
    """Render the dashboard: day x project x family rows, factors applied in-page."""
    rows = defaultdict(lambda: {c: 0 for c in COUNTS})
    for r in records:
        row = rows[(r["day"], r["project"], model_family(r["model"]))]
        for c in COUNTS:
            row[c] += r[c]
    data = {
        "generated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "families": FAMILIES,
        "co2_g_per_wh": CO2_G_PER_WH,
        "scales": {"cache_write": CACHE_WRITE_SCALE, "cache_read": CACHE_READ_SCALE},
        "comparisons": COMPARISONS,
        "rows": [{"day": d, "project": p, "family": f, **r}
                 for (d, p, f), r in sorted(rows.items())],
    }
    html = TEMPLATE.read_text().replace(
        "/*DATA*/null", json.dumps(data).replace("</", "<\\/"))
    out_path = Path(out_path)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(html)
    tmp.replace(out_path)


def print_table(result, by=None, unknown=()):
    today = datetime.now().astimezone().date().isoformat()
    for name, row in result.items():
        print(f"\n== {name}  ({row['calls']:,} API calls)")
        print(f"{'type':<12}{'tokens':>16}{'Wh':>12}{'g CO2e':>12}{'share':>8}")
        total = row["g_co2e"]["total"] or 1
        for t in TYPES:
            print(f"{t:<12}{row['tokens'][t]:>16,}{row['wh'][t]:>12.1f}"
                  f"{row['g_co2e'][t]:>12.1f}{row['g_co2e'][t] / total:>8.0%}")
        print(f"{'total':<12}{sum(row['tokens'].values()):>16,}"
              f"{row['wh']['total']:>12.1f}{row['g_co2e']['total']:>12.1f}")
        key = name if by == "day" else today
        print(f"\u2248 {format_comparison(row['g_co2e']['total'] / 1000, key)}")
    print(f"\nTokenClimate factors per model family; cache write x{CACHE_WRITE_SCALE}, "
          f"cache read x{CACHE_READ_SCALE} of input.", file=sys.stderr)
    if unknown:
        print(f"Counted as {FAMILIES[FALLBACK_FAMILY]['label']}: {', '.join(sorted(unknown))}",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="ISO date, e.g. 2026-09-01")
    ap.add_argument("--project", help="substring of the project name")
    ap.add_argument("--by", choices=["project", "session", "model", "family", "day"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH", help="write the dashboard to PATH")
    args = ap.parse_args()
    records = select(update_history(), args.project, args.since)
    if args.html:
        write_html(records, args.html)
    elif args.json:
        json.dump({"factors_per_million": factor_table(), "groups": tally(records, args.by)},
                  sys.stdout, indent=2)
        print()
    else:
        unknown = {r["model"] for r in records if not is_known_family(r["model"])}
        print_table(tally(records, args.by), args.by, unknown)


if __name__ == "__main__":
    main()
