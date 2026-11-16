#!/usr/bin/env python3
"""Tally Claude Code token usage by type and estimate energy and CO2e.

Reads the local transcripts in ~/.claude/projects (including subagent
transcripts), deduplicates API calls by message id, and splits usage into
uncached input, cache writes, cache reads and output.

Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default), so
every run also folds the totals into a history file (history.json in the data
directory, ~/.claude/carbon by default). Totals are kept per session, day and
model and git branch; a record whose transcript has been deleted keeps its last
value. All reports read from that history, so they keep covering days whose
transcripts are gone. The history holds token counts, the branch and the
working directory only, never conversation text.

Energy and emission factors follow the TokenClimate methodology
(tokenclimate-v3-2026-10, methodology v4.26, https://tokenclimate.com/en/methodology): per-family
server energy for input and output tokens (Haiku, Sonnet, Opus, Fable), cache
writes at the input rate, cache reads at 0.08 times it, and CO2e = energy x
(PUE x grid intensity + embodied hardware carbon). Models that match no family
are counted as Opus and reported as such.

Usage:
    token_carbon.py                        # all history, all projects
    token_carbon.py --since 2026-09-01     # only days on or after this date
    token_carbon.py --until 2026-09-30     # only days on or before this date
    token_carbon.py --project myproject    # substring match on project name
    token_carbon.py --by project           # also: session, model, family, day, branch
    token_carbon.py --json                 # machine-readable output
    token_carbon.py --html dashboard.html  # self-contained dashboard page
"""

import argparse
import fcntl
import json
import math
import os
import pickle
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_CARBON_HOME", Path.home() / ".claude" / "carbon"))
PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))
TEMPLATE = Path(__file__).resolve().parent / "dashboard_template.html"
HISTORY_VERSION = 1

FACTOR_SET = "tokenclimate-v3-2026-10"
METHODOLOGY = "v4.26-methodology-2026-11"

# Server (IT) energy in Wh per million tokens, TokenClimate tokenclimate-v3-2026-10
# methodology, "Anthropic parameters". Only Sonnet is fitted to a published
# estimate; Opus is 2x Sonnet, Haiku 0.25x (0.5x before v4.15) and Fable 2x Opus,
# hence the confidence levels.
FAMILIES = {
    "haiku": {"label": "Claude Haiku", "in_wh": 30.6, "out_wh": 633, "confidence": "medium",
              "provider": "anthropic"},
    "sonnet": {"label": "Claude Sonnet", "in_wh": 119, "out_wh": 2525, "confidence": "high",
               "provider": "anthropic"},
    "opus": {"label": "Claude Opus", "in_wh": 238, "out_wh": 5050, "confidence": "medium",
             "provider": "anthropic"},
    "fable": {"label": "Claude Fable", "in_wh": 476, "out_wh": 10100, "confidence": "low",
              "provider": "anthropic"},
}
FALLBACK_FAMILY = "opus"

# Per-model rows TokenClimate publishes for other providers (methodology page,
# "Other providers' per-token energy", v4.26; Wh per million tokens, IT energy,
# EcoLogits 0.11.3 unless noted). Matched by model-id prefix, longest first, so
# "gemini-3.8-flash-tiered" finds "gemini-3.8-flash". Models not listed here
# fall back to an assumed Claude class (see carbon_gemini and carbon_vscode).
# Rows here are used as published, never mapped to a Claude class.
MODEL_ROWS = {
    "gemini-4-argon": (7431, 44588, "google", "low"),      # price proxy, v4.20
    "gemini-3.8-flash": (681, 4084, "google", "low"),
    "gemini-3.7-flash": (683, 4096, "google", "low"),
    "gemini-3.6-flash": (665, 3991, "google", "low"),
    "gemini-3.5-flash-lite": (56, 335, "google", "low"),
    "gemini-3.5-flash": (765, 4589, "google", "low"),
    "gemini-3.1-flash-lite": (57, 340, "google", "low"),
    "gemini-2.5-flash": (267, 1602, "google", "low"),
    "gemini-2.5-pro": (2938, 17625, "google", "low"),
    "gpt-5.6-sol": (857, 5144, "openai", "low"),
    "gpt-5.6-terra": (279, 1676, "openai", "low"),
    "gpt-5.6-luna": (98, 589, "openai", "low"),
    "gpt-5.5": (845, 5067, "openai", "low"),
    "gpt-5.4-mini": (72, 430, "openai", "low"),
    "gpt-5.4-nano": (23, 136, "openai", "low"),
    "gpt-5.4": (290, 1741, "openai", "low"),
}

# Provider infrastructure rows (TokenClimate "Provider infrastructure"):
# (PUE, grid intensity in kg CO2e/kWh). Embodied hardware carbon is one
# constant, 49 g CO2e per kWh of IT energy (v4.7; range 22-66). TokenClimate
# prints it in the Anthropic row and says its range applies to every provider
# (and its v4.7 entry moved OpenAI and Google CO2e), so it is applied to all.
PROVIDERS = {"anthropic": (1.14, 0.287), "google": (1.09, 0.345), "openai": (1.12, 0.35)}

# CO2e per Wh of server energy: datacentre overhead (PUE) times grid carbon
# intensity, plus amortized hardware (embodied) carbon. AWS parameters from
# TokenClimate: PUE 1.14, 0.287 kg/kWh, 49 g/kWh.
EMBODIED_G_PER_WH = 0.049
PUE, GRID_G_PER_WH = PROVIDERS["anthropic"]
CO2_G_PER_WH = PUE * GRID_G_PER_WH + EMBODIED_G_PER_WH  # 0.37618


def provider_pue(provider="anthropic"):
    return PROVIDERS[provider][0]


def co2_g_per_wh(provider="anthropic", embodied=True):
    """g CO2e per server Wh: PUE x grid, plus embodied carbon unless excluded.

    embodied=False is the operational-only boundary (use-phase electricity).
    """
    pue, grid = PROVIDERS[provider]
    return pue * grid + (EMBODIED_G_PER_WH if embodied else 0.0)

# Relative to uncached input. A cache write runs the same prefill as input; a
# cache read reuses stored keys and values and costs 0.08x (TokenClimate,
# "Cache energy"; plausible range 0.05 to 0.20).
CACHE_WRITE_SCALE = 1.0
CACHE_READ_SCALE = 0.08

TYPES = ("input", "cache_write", "cache_read", "output")
COUNTS = TYPES + ("calls",)


def model_family(model):
    """'claude-sonnet-5-5' -> 'sonnet'. Unrecognized models count as Opus."""
    m = model.lower()
    for family in FAMILIES:
        if family in m:
            return family
    return "fable" if "mythos" in m else FALLBACK_FAMILY


def listed_row(model):
    """Key of the TokenClimate-published row for a model id, or None."""
    m = model.lower().rsplit("/", 1)[-1]
    for key in sorted(MODEL_ROWS, key=len, reverse=True):
        if m.startswith(key):
            return key
    return None


def provider_of(model):
    """Provider whose infrastructure row applies to a model id (Anthropic if unknown)."""
    m = model.lower().rsplit("/", 1)[-1]
    if m.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    return "anthropic"


def factor(key):
    """Factor dict (in_wh, out_wh, provider, ...) for a Claude family or a listed model row."""
    if key in FAMILIES:
        return FAMILIES[key]
    i, o, provider, conf = MODEL_ROWS[key]
    return {"label": key, "in_wh": i, "out_wh": o, "confidence": conf, "provider": provider}


def wh_per_mtok(family, token_type):
    f = factor(family)
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


def record_key(session, day, model, branch):
    return f"{session}|{day}|{model}|{branch}"


CACHE_VERSION = 1


def parse_transcript(path):
    """Final API calls in one transcript: ({id: fields}, cwds). Pure in the file."""
    calls, cwds = {}, set()
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
                cwds.add(rec["cwd"])
            # Streaming writes one line per content block with the same
            # id; the last one carries the final output count.
            calls[msg.get("id") or rec.get("requestId")] = (
                rec.get("sessionId"), rec.get("timestamp", "")[:10],
                msg.get("model", "unknown"), rec.get("gitBranch") or "-",
                rec.get("cwd", ""),
                usage.get("input_tokens", 0) or 0,
                usage.get("cache_creation_input_tokens", 0) or 0,
                usage.get("cache_read_input_tokens", 0) or 0,
                usage.get("output_tokens", 0) or 0)
    return calls, cwds


def scan_transcripts(projects=PROJECTS, cache_path=None):
    """Totals from the transcripts on disk, keyed by session|day|model|branch.

    The branch is the transcript's gitBranch at the time of each call ("-"
    outside a git repository), so a session that switches branch splits
    into one record per branch.

    Parsing a transcript is the slow part, so each file's calls are cached in
    cache_path keyed by relative path, size and mtime; only new or changed
    files are re-read. Deduplication across files stays outside the cache.
    """
    cache = {}
    if cache_path is not None and Path(cache_path).exists():
        try:
            with open(cache_path, "rb") as fh:
                loaded = pickle.load(fh)
            if loaded.get("version") == CACHE_VERSION:
                cache = loaded["files"]
        except Exception:
            cache = {}
    fresh, records, seen, cwds = {}, {}, set(), defaultdict(set)
    for path in sorted(projects.rglob("*.jsonl")):
        rel = str(path.relative_to(projects))
        project = path.relative_to(projects).parts[0]
        st = path.stat()
        sig = (st.st_size, st.st_mtime_ns)
        hit = cache.get(rel)
        if hit and hit[0] == sig:
            calls, file_cwds = hit[1], hit[2]
        else:
            calls, file_cwds = parse_transcript(path)
        fresh[rel] = (sig, calls, file_cwds)
        cwds[project] |= file_cwds
        for mid, (sess, day, model, branch, cwd, i, cw, cr, o) in calls.items():
            if mid in seen:
                continue
            seen.add(mid)
            session = sess or path.stem
            r = records.setdefault(record_key(session, day, model, branch), {
                "session": session, "day": day, "model": model, "branch": branch,
                "project_dir": project, **{c: 0 for c in COUNTS},
            })
            r["cwd"] = cwd
            r["calls"] += 1
            r["input"] += i
            r["cache_write"] += cw
            r["cache_read"] += cr
            r["output"] += o
    if cache_path is not None and fresh != cache:
        tmp = Path(f"{cache_path}.{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            pickle.dump({"version": CACHE_VERSION,
                         "files": {k: (v[0], v[1], v[2]) for k, v in fresh.items()}}, fh)
        tmp.replace(cache_path)
    for r in records.values():
        r["project"] = project_name(r["project_dir"], sorted(cwds[r["project_dir"]]))
    return records


def migrate(history):
    """Rekey records from before branches were kept, in place.

    Old keys are session|day|model. They become session|day|model|? and keep
    their counts under "legacy", so merge() can tell how much of them the
    branch-keyed records now cover.
    """
    for key in [k for k in history if k.count("|") == 2]:
        r = history.pop(key)
        r.update(branch="?", cwd=r.get("cwd", ""), legacy={c: r.get(c, 0) for c in COUNTS})
        history[f"{key}|?"] = r
    return history


def merge(history, current):
    """Fold current transcript totals into the history, in place.

    A transcript only grows until it is deleted, so each count keeps the
    larger of its stored and current values. Records with no transcript left
    keep what was stored.

    A migrated record (branch "?") holds whatever its legacy counts exceed the
    branch records of the same session, day and model, so each count for that
    group totals max(legacy, sum over branches): rescanning a transcript that
    still exists moves its counts to their branches without double counting,
    and a deleted one keeps its old total under "?".
    """
    migrate(history)
    for key, cur in current.items():
        old = history.get(key)
        if old is None:
            history[key] = cur
            continue
        for c in COUNTS:
            old[c] = max(old.get(c, 0), cur[c])
        old["project"] = cur["project"]
        old["cwd"] = cur.get("cwd", old.get("cwd", ""))
    covered = defaultdict(lambda: {c: 0 for c in COUNTS})
    for key, r in history.items():
        if "legacy" not in r:
            for c in COUNTS:
                covered[key.rsplit("|", 1)[0]][c] += r.get(c, 0)
    for key, r in history.items():
        if "legacy" in r:
            done = covered.get(key.rsplit("|", 1)[0], {})
            for c in COUNTS:
                r[c] = max(0, r["legacy"][c] - done.get(c, 0))
    return history


def update_history(data_dir=DATA_DIR, projects=PROJECTS):
    """Scan transcripts, merge into history.json and return all records."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "history.json"
    # Several sessions can finish a turn at once; serialize the read-merge-write.
    with open(data_dir / "history.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = {"version": HISTORY_VERSION, "records": {}}
        if path.exists():
            stored = json.loads(path.read_text())
            if stored.get("version") != HISTORY_VERSION:
                sys.exit(f"{path}: unknown history version {stored.get('version')}")
        history = stored["records"]
        merge(history, scan_transcripts(projects, data_dir / "scan_cache.pickle"))
        tmp = path.with_suffix(".tmp")
        # Keep any other top-level keys a newer version may have written.
        tmp.write_text(json.dumps(stored, indent=1))
        tmp.replace(path)
    return list(history.values())


def select(records, project=None, since=None, until=None):
    """Records for a project and day range; migrated records left empty drop out."""
    return [r for r in records
            if not r["model"].startswith("<")
            and any(r.get(c, 0) for c in COUNTS)
            and (not project or project in r["project"] or project in r["project_dir"])
            and (not since or r["day"] >= since)
            and (not until or r["day"] <= until)]


def tally(records, by=None, embodied=True):
    zero = lambda: {t: 0.0 for t in TYPES}
    groups = defaultdict(lambda: {"calls": 0, "tokens": zero(), "wh": zero(), "g_co2e": zero()})
    for r in records:
        family = model_family(r["model"])
        g = groups[FAMILIES[family]["label"] if by == "family"
                   else r.get(by, "?") if by else "total"]
        g["calls"] += r["calls"]
        for t in TYPES:
            wh = r[t] / 1e6 * wh_per_mtok(family, t)
            g["tokens"][t] += r[t]
            g["wh"][t] += wh
            g["g_co2e"][t] += wh * co2_g_per_wh(embodied=embodied)
    for g in groups.values():
        g["tokens"] = {t: int(v) for t, v in g["tokens"].items()}
        g["wh"]["total"] = sum(g["wh"][t] for t in TYPES)
        g["g_co2e"]["total"] = sum(g["g_co2e"][t] for t in TYPES)
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]["g_co2e"]["total"]))


def factor_table(embodied=True):
    """Wh and g CO2e per million tokens, by family and token type."""
    k = co2_g_per_wh(embodied=embodied)
    return {f: {t: {"wh": wh_per_mtok(f, t), "g_co2e": wh_per_mtok(f, t) * k}
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


DEBOUNCE_S = 5 * 60       # the Stop hook fires every turn; a dashboard this fresh is fine
STALE_LOCK_S = 30 * 60    # a run never legitimately takes this long


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_run_lock(lock_dir):
    """Take the single-instance lock (an atomic mkdir); False if a live run holds it."""
    for _ in range(2):
        try:
            os.mkdir(lock_dir)
            (Path(lock_dir) / "pid").write_text(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                age = time.time() - os.stat(lock_dir).st_mtime
                pid = int((Path(lock_dir) / "pid").read_text().strip() or 0)
            except (OSError, ValueError):
                pid, age = 0, 0
                # pid file not written yet: give the owner a moment, then treat as stale
                try:
                    age = time.time() - os.stat(lock_dir).st_mtime
                except OSError:
                    continue
                if age < 10:
                    return False
            if pid and pid_alive(pid) and age < STALE_LOCK_S:
                return False
            shutil.rmtree(lock_dir, ignore_errors=True)  # owner dead or lock stale
    return False


def release_run_lock(lock_dir):
    shutil.rmtree(lock_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="ISO date, e.g. 2026-09-01")
    ap.add_argument("--until", help="ISO date, inclusive")
    ap.add_argument("--project", help="substring of the project name")
    ap.add_argument("--by", choices=["project", "session", "model", "family", "day", "branch"])
    ap.add_argument("--operational-only", action="store_true",
                    help="exclude embodied hardware carbon from CO2e (use-phase electricity only)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH", help="write the dashboard to PATH")
    ap.add_argument("--force", action="store_true",
                    help="with --html: ignore the debounce and a running instance")
    args = ap.parse_args()
    if args.html and not args.force:
        out = Path(args.html)
        if out.exists() and time.time() - out.stat().st_mtime < DEBOUNCE_S:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock_dir = DATA_DIR / "run.lock.d"
        if not acquire_run_lock(lock_dir):
            return
        try:
            return run(args)
        finally:
            release_run_lock(lock_dir)
    run(args)


def run(args):
    records = select(update_history(), args.project, args.since, args.until)
    if args.html:
        write_html(records, args.html)
    elif args.json:
        fields = ("session", "day", "model", "branch", "cwd", "project") + COUNTS
        rows = [{f: r.get(f, "") for f in fields}
                for r in sorted(records, key=lambda r: (r["day"], r["session"], r["model"]))]
        emb = not args.operational_only
        json.dump({"factor_set": FACTOR_SET, "embodied_included": emb,
                   "factors_per_million": factor_table(emb),
                   "groups": tally(records, args.by, emb),
                   "records": rows}, sys.stdout, indent=2)
        print()
    else:
        unknown = {r["model"] for r in records if not is_known_family(r["model"])}
        print_table(tally(records, args.by, not args.operational_only), args.by, unknown)


if __name__ == "__main__":
    main()
