"""GitHub Actions: runner time for a repository's CI workflows (tests, docs, lint ...).

Lists every workflow run with `gh api repos/<slug>/actions/runs`, skips
Copilot's own workflows (carbon_copilot covers those), and for each finished
run sums the durations of its jobs, all attempts included, from
/actions/runs/<id>/jobs. Runner minutes are what draw power: a matrix of six
jobs runs six runners at once, so a run's wall-clock duration would
undercount. Public repositories are not billed, so billable minutes read 0.

Automatic Claude PR reviews (claude-code-action) are also costed for their LLM
tokens: see "Claude reviews" below.

Energy follows Green Algorithms (Lannelongue, Grealey & Inouye 2021):
t x (cores x P_core x u + memory x 0.3725 W/GB) x PUE, with the runner
parameters below.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import carbon_common as cc
import token_carbon as tc

# GitHub-hosted runner hardware, by the OS in the job's runner label. The
# standard Linux and Windows runners for public repositories have 4 vCPUs and
# 16 GB; they run on Azure, typically AMD EPYC 7763 (280 W TDP, 64 cores, so
# 4.375 W per core, counting a vCPU as a core, which errs high). macOS arm64
# runners have 3 M1 cores and 7 GB; Green Algorithms lists the M1 at about
# 2.5 W per core (20 W, 8 cores).
RUNNERS = {
    "ubuntu": {"cores": 4, "p_core_w": 280 / 64, "mem_gb": 16},
    "windows": {"cores": 4, "p_core_w": 280 / 64, "mem_gb": 16},
    "macos": {"cores": 3, "p_core_w": 2.5, "mem_gb": 7},
}

# CPU usage (low, mid, high): CI jobs are not measured; Green Algorithms takes
# 1.0 when usage is unknown, and 0.5 is the low bound. PUE and grid intensity
# are assumptions for an Azure region in the US: a hyperscale PUE of 1.18 and
# the US average grid, 0.37 kg CO2e/kWh (EPA eGRID2022).
PARAMS = {"u_cpu": (0.5, 1.0, 1.0), "pue": 1.18, "grid_kg_per_kwh": 0.37}

# Copilot's own workflows (its agent and reviewer), costed by carbon_copilot.
COPILOT_PATH = re.compile(r"copilot-swe-agent|copilot-pull-request-reviewer")


# --- Claude reviews (LLM tokens) ---------------------------------------------
#
# claude-code-action prints the Claude Code result object in the job log. Its
# `modelUsage` lists the models used but, as published to the log, carries no
# token counts; it gives `total_cost_usd` and `num_turns`. If a log ever holds
# real token counts they are used (method "measured"). Otherwise tokens are
# estimated (method "estimated") from total_cost_usd: the dollars are split
# over input, cache-write, cache-read and output tokens by a cost mix measured
# on this dashboard's own local Claude Code history for the same model family,
# and each share is divided by Anthropic's list price for that token type.
CLAUDE_REVIEW_PATH = re.compile(r"claude-review\.yml$")
REVIEW_JOB = "review"
DEFAULT_REVIEW_MODEL = "claude-opus-5-5"   # the --model in the repositories' workflows

# List prices in USD per million tokens (input, cache write, cache read,
# output), Anthropic API pricing, read 2026-11-17. Cache writes are the
# 5-minute rate. A model not listed takes its family's current row.
PRICES = {
    "opus": (4.00, 5.00, 0.20, 20.00),     # claude-opus-5-5
    "sonnet": (2.00, 2.50, 0.10, 10.00),   # claude-sonnet-5-5
    "haiku": (0.10, 0.125, 0.01, 0.50),    # claude-haiku-5-5
}

# Share of a session's USD cost in (input, cache write, cache read, output)
# tokens, from the local ledger (mix_from_history). low and high are the pooled
# sessions in the bottom and top decile of server Wh per USD (the most
# cache-heavy and the most output-heavy mixes); mid pools every session.
# 552 Opus and 539 Sonnet sessions of at least USD 1, valued at the prices
# above. Haiku has too few sessions and uses the Opus mix.
MIX = {
    "opus": {"low": (0.0043, 0.6519, 0.2871, 0.0567), "mid": (0.0007, 0.2934, 0.5384, 0.1675),
             "high": (0.0001, 0.2499, 0.4087, 0.3413)},
    "sonnet": {"low": (0.0011, 0.7275, 0.2495, 0.0219), "mid": (0.0006, 0.3684, 0.4382, 0.1928),
               "high": (0.0001, 0.2282, 0.4289, 0.3427)},
}
CACHE_READ_RANGE = (0.05, tc.CACHE_READ_SCALE, 0.20)
LLM_LABEL = "Claude review (LLM, {})"


def mix_from_history(records, family, min_usd=1.0, decile=0.1):
    """Cost mixes {"low", "mid", "high"} from token_carbon history records of a family.

    Each record's tokens are valued at PRICES[family]; sessions below min_usd are
    skipped; low and high pool the sessions in the bottom and top `decile` of
    server Wh per USD. Returns None if no session qualifies. This is how MIX
    was derived.
    """
    rows = []
    for r in records:
        if r["model"].startswith("<") or tc.model_family(r["model"]) != family:
            continue
        usd = [r.get(t, 0) * p / 1e6 for t, p in zip(tc.TYPES, PRICES[family])]
        if sum(usd) >= min_usd:
            wh = sum(r.get(t, 0) / 1e6 * tc.wh_per_mtok(family, t) for t in tc.TYPES)
            rows.append((wh / sum(usd), usd))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    k = max(1, int(len(rows) * decile))

    def share(sel):
        tot = [sum(u[i] for _, u in sel) for i in range(4)]
        return tuple(round(x / sum(tot), 4) for x in tot)
    return {"low": share(rows[:k]), "mid": share(rows), "high": share(rows[-k:])}


def parse_review_log(text):
    """The Claude Code result object in a review job log, as a flat record, or None.

    Returns {"llm_cost_usd", "llm_turns", "llm_model", "llm_models", "llm_usage"}:
    llm_usage is {model: {input, cache_write, cache_read, output}} when the log
    holds real token counts, else {}.
    """
    lines = []
    for line in text.splitlines():
        # job logs carry an ISO timestamp (and, from `run view`, job and step names)
        m = re.search(r"\d{4}-\d\d-\d\dT[\d:.]+Z ?", line)
        lines.append(line[m.end():] if m else line)
    body = "\n".join(lines)
    result = None
    for m in re.finditer(r'^\{\n  "type": "result"', body, re.M):
        try:
            result = json.JSONDecoder().raw_decode(body[m.start():])[0]
        except ValueError:
            continue
    if not result or result.get("total_cost_usd") is None:
        return None
    init = re.search(r'"subtype": "init",\n(?:.*\n)*?\s*"model": "([^"]+)"', body)
    usage = {}
    for model, u in (result.get("modelUsage") or {}).items():
        counts = {"input": u.get("inputTokens", 0), "cache_write": u.get("cacheCreationInputTokens", 0),
                  "cache_read": u.get("cacheReadInputTokens", 0), "output": u.get("outputTokens", 0)}
        if any(counts.values()):
            usage[model] = counts
    if not usage and any((result.get("usage") or {}).get(k) for k in
                         ("input_tokens", "output_tokens", "cache_read_input_tokens")):
        u = result["usage"]
        usage[init.group(1) if init else DEFAULT_REVIEW_MODEL] = {
            "input": u.get("input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0)}
    return {"llm_cost_usd": float(result["total_cost_usd"]), "llm_turns": result.get("num_turns", 0),
            "llm_model": init.group(1) if init else "", "llm_models": sorted(result.get("modelUsage") or {}),
            "llm_usage": usage}


def estimate_tokens(cost_usd, model, which="mid"):
    """Token counts for a review costing cost_usd, from the family's cost mix (an estimate)."""
    family = tc.model_family(model)
    mix = MIX.get(family, MIX["opus"])[which]
    price = PRICES.get(family, PRICES["opus"])
    return {t: cost_usd * share / p * 1e6 for t, share, p in zip(tc.TYPES, mix, price)}


def review_item(key, r, embodied=None):
    """The LLM cost item of one review run record, or None if it has no result."""
    if r.get("llm_cost_usd") is None:
        return None
    model = r.get("llm_model") or DEFAULT_REVIEW_MODEL
    usage = r.get("llm_usage") or {}
    kwh, kg = [], []
    if usage:
        method = "measured"
        family = tc.model_family(next(iter(usage)))
        tokens = {t: sum(c.get(t, 0) for c in usage.values()) for t in tc.TYPES}
        for scale in CACHE_READ_RANGE:
            e = cc.llm_cost(family, tokens, cache_read_scale=scale, embodied=embodied)
            kwh.append(e[0])
            kg.append(e[1])
    else:
        method = "estimated"
        family = tc.model_family(model)
        tokens = estimate_tokens(r["llm_cost_usd"], model)
        for which in ("low", "mid", "high"):
            e = cc.llm_cost(family, estimate_tokens(r["llm_cost_usd"], model, which),
                            embodied=embodied)
            kwh.append(e[0])
            kg.append(e[1])
    return {
        "source": "gha", "id": f"{key}#llm", "day": r["day"], "label": LLM_LABEL.format(method),
        "kind": "model calls", "branch": r["head_branch"], "pr": r.get("pr"), "sha": r.get("head_sha"),
        "class": "dev", "method": method, "model": model, "cost_usd": r["llm_cost_usd"],
        "tokens": {t: round(v) for t, v in tokens.items()}, "calls": r.get("llm_turns", 0),
        "kg_by_type": {t: cc.llm_cost(family, {t: tokens[t]}, embodied=embodied)[1]
                       for t in tc.TYPES},
        "kwh": kwh, "kg": kg,
    }


def seconds(start, end):
    if not start or not end:
        return 0.0
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return max(0.0, (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds())


def runner_os(labels):
    text = " ".join(labels).lower()
    return next((os_ for os_ in RUNNERS if os_ in text), "ubuntu")


def pr_number(run):
    """A run's PR: its pull_requests list, a refs/pull/<n>/head branch or 'PR #n' title."""
    if run.get("prs"):
        return run["prs"][0]
    for text, pattern in ((run.get("head_branch"), r"^refs/pull/(\d+)/"),
                          (run.get("title"), r"PR #(\d+)")):
        m = re.search(pattern, text or "")
        if m:
            return int(m.group(1))
    return None


RUN_JQ = ("{id, workflow_id, name, title: .display_title, head_branch, head_sha, event, status,"
          " conclusion, created_at, run_started_at, updated_at, run_attempt,"
          " prs: [.pull_requests[].number]}")


def list_runs(slug, workflow_id=None):
    path = (f"repos/{slug}/actions/workflows/{workflow_id}/runs?per_page=100" if workflow_id
            else f"repos/{slug}/actions/runs?per_page=100")
    return cc.gh_json(path, paginate=True, jq=f".workflow_runs[] | {RUN_JQ}")


def workflows(slug):
    """{workflow id: (name, path)} for a repository."""
    rows = cc.gh_json(f"repos/{slug}/actions/workflows?per_page=100", paginate=True,
                      jq=".workflows[] | {id, name, path}") or []
    return {w["id"]: (w["name"], w["path"]) for w in rows}


def job_summary(slug, run_id):
    jobs = cc.gh_json(f"repos/{slug}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
                      paginate=True, jq=".jobs[] | {id, name, conclusion, started_at, completed_at, labels}")
    if jobs is None:
        return None
    by_os = {}
    for j in jobs:
        os_ = runner_os(j.get("labels") or [])
        by_os[os_] = by_os.get(os_, 0.0) + seconds(j.get("started_at"), j.get("completed_at"))
    review = next((j["id"] for j in jobs if j.get("name") == REVIEW_JOB
                   and j.get("conclusion") == "success"), 0)
    return {"jobs": len(jobs), "runner_s": by_os, "review_job": review}


def review_record(slug, job_id):
    """parse_review_log of a job's log, or None if the log is gone or has no result."""
    # Job logs hold ANSI colour codes, which gh refuses to print without the flag.
    text = cc.run(["gh", "api", "--allow-escape-sequences", f"repos/{slug}/actions/jobs/{job_id}/logs"],
                  timeout=300)
    return parse_review_log(text) if text else None


def collect(slug, known=None, workers=8):
    """{run id: record} for the finished non-Copilot runs of slug.

    Runs already in known (the ledger) with job data are not fetched again.
    """
    known = known or {}
    flows = workflows(slug)
    runs = list_runs(slug)
    if runs is None:
        return {}
    records = {}
    for run in runs:
        name, path = flows.get(run["workflow_id"], (run.get("name", ""), ""))
        if COPILOT_PATH.search(path) or run.get("status") != "completed":
            continue
        records[f"{slug}#{run['id']}"] = {
            "repo": slug, "run_id": run["id"], "workflow": name, "path": path,
            "title": run.get("title", ""),
            "day": cc.iso_day(run.get("run_started_at") or run.get("created_at")),
            "head_branch": run.get("head_branch") or "", "head_sha": run.get("head_sha") or "",
            "event": run.get("event", ""), "conclusion": run.get("conclusion") or "",
            "pr": pr_number(run), "attempts": run.get("run_attempt") or 1,
            "wall_s": seconds(run.get("run_started_at"), run.get("updated_at")),
        }
    # A Claude review run is also looked at once for its LLM result, so a ledger
    # from before the llm_* fields is filled in while GitHub keeps the logs.
    todo = [k for k in records if "runner_s" not in known.get(k, {})
            or (CLAUDE_REVIEW_PATH.search(records[k]["path"])
                and not known.get(k, {}).get("llm_checked"))]
    with ThreadPoolExecutor(workers) as pool:
        for key, summary in zip(todo, pool.map(lambda k: job_summary(slug, records[k]["run_id"]), todo)):
            if summary:
                records[key].update(summary)
    reviews = [k for k in todo if records[k].get("review_job")]
    with ThreadPoolExecutor(workers) as pool:
        for key, rec in zip(reviews, pool.map(
                lambda k: review_record(slug, records[k]["review_job"]), reviews)):
            if rec:
                records[key].update(rec)
    for key in todo:
        r = records[key]
        if CLAUDE_REVIEW_PATH.search(r["path"]) and "review_job" in r \
                and (not r["review_job"] or "llm_cost_usd" in r):
            r["llm_checked"] = True   # no successful review job, or its result read
    return records


def runner_kwh(runner_s, u_cpu, pue):
    """Green Algorithms energy for {os: seconds} of runner time."""
    total = 0.0
    for os_, s in runner_s.items():
        r = RUNNERS.get(os_, RUNNERS["ubuntu"])
        total += cc.green_algorithms_kwh(s / 3600, n_cpu=r["cores"], p_core_w=r["p_core_w"],
                                         u_cpu=u_cpu, mem_gb=r["mem_gb"], pue=pue)
    return total


def cost(records, params=PARAMS, embodied=None):
    items = []
    for key, r in sorted(records.items()):
        if COPILOT_PATH.search(r.get("path", "")):
            continue
        # A run whose jobs were never fetched counts its wall time on one
        # runner. A run whose jobs were fetched and that had none (cancelled
        # or failed before a runner started, or awaiting approval) used no
        # runner: its wall time, which runs to updated_at, can span days.
        if "jobs" in r or "runner_s" in r:
            runner_s = r.get("runner_s") or {}
        else:
            runner_s = {"ubuntu": r.get("wall_s", 0.0)}
        kwh = [runner_kwh(runner_s, u, params["pue"]) for u in params["u_cpu"]]
        items.append({
            "source": "gha", "id": key, "day": r["day"], "label": r["workflow"],
            "kind": "CI", "branch": r["head_branch"], "pr": r.get("pr"), "sha": r.get("head_sha"),
            "class": "dev", "runner_min": sum(runner_s.values()) / 60,
            "kwh": kwh, "kg": [k * params["grid_kg_per_kwh"] for k in kwh],
        })
        llm = review_item(key, r, embodied)
        if llm:
            items.append(llm)
    return items
