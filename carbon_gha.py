"""GitHub Actions: runner time for a repository's CI workflows (tests, docs, lint ...).

Lists every workflow run with `gh api repos/<slug>/actions/runs`, skips
Copilot's own workflows (carbon_copilot covers those), and for each finished
run sums the durations of its jobs, all attempts included, from
/actions/runs/<id>/jobs. Runner minutes are what draw power: a matrix of six
jobs runs six runners at once, so a run's wall-clock duration would
undercount. Public repositories are not billed, so billable minutes read 0.

Energy follows Green Algorithms (Lannelongue, Grealey & Inouye 2021):
t x (cores x P_core x u + memory x 0.3725 W/GB) x PUE, with the runner
parameters below.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import carbon_common as cc

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
                      paginate=True, jq=".jobs[] | {started_at, completed_at, labels}")
    if jobs is None:
        return None
    by_os = {}
    for j in jobs:
        os_ = runner_os(j.get("labels") or [])
        by_os[os_] = by_os.get(os_, 0.0) + seconds(j.get("started_at"), j.get("completed_at"))
    return {"jobs": len(jobs), "runner_s": by_os}


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
    todo = [k for k in records if "runner_s" not in known.get(k, {})]
    with ThreadPoolExecutor(workers) as pool:
        for key, summary in zip(todo, pool.map(lambda k: job_summary(slug, records[k]["run_id"]), todo)):
            if summary:
                records[key].update(summary)
    return records


def runner_kwh(runner_s, u_cpu, pue):
    """Green Algorithms energy for {os: seconds} of runner time."""
    total = 0.0
    for os_, s in runner_s.items():
        r = RUNNERS.get(os_, RUNNERS["ubuntu"])
        total += cc.green_algorithms_kwh(s / 3600, n_cpu=r["cores"], p_core_w=r["p_core_w"],
                                         u_cpu=u_cpu, mem_gb=r["mem_gb"], pue=pue)
    return total


def cost(records, params=PARAMS):
    items = []
    for key, r in sorted(records.items()):
        if COPILOT_PATH.search(r.get("path", "")):
            continue
        # A run whose jobs were never fetched counts its wall time on one runner.
        runner_s = r.get("runner_s") or {"ubuntu": r.get("wall_s", 0.0)}
        kwh = [runner_kwh(runner_s, u, params["pue"]) for u in params["u_cpu"]]
        items.append({
            "source": "gha", "id": key, "day": r["day"], "label": r["workflow"],
            "kind": "CI", "branch": r["head_branch"], "pr": r.get("pr"), "sha": r.get("head_sha"),
            "class": "dev", "runner_min": sum(runner_s.values()) / 60,
            "kwh": kwh, "kg": [k * params["grid_kg_per_kwh"] for k in kwh],
        })
    return items
