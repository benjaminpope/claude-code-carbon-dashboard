"""GitHub Copilot in the cloud: the coding agent and pull-request review.

Two kinds of telemetry, neither with token counts:

- Workflow runs. Copilot's agent ("Copilot cloud agent") and reviewer
  ("Copilot", "Copilot code review") appear as dynamic Actions workflows.
  Each run gives its duration (/actions/runs/<id>/timing run_duration_ms;
  billable time reads 0), head branch and PR.
- Billing. /users/<user>/settings/billing/usage has one row per day, SKU and
  repository. Up to May 2026 Copilot is metered in premium requests (one per
  prompt; /settings/billing/premium_request/usage splits them by model,
  including "Code Review model" and "Coding Agent model"); from June in AI
  Credits, which are token-metered at list price, so they track tokens
  better than anything else available. Both endpoints need a token with
  the user scope (`gh auth refresh -h github.com -s user`); without it they
  return 404 and only the run-time estimate is made.

Billing rows are account-wide unless repositoryName is set (from about
October 2026), and they include local VS Code chat, which carbon_vscode
already counts from its own logs. So, per month (see attribute_month):

- "Copilot Cloud Agent" credits are the agent's. A row with no repository is
  attributed by the repository's share of the account's agent PRs that month.
- Other AI Credits, less the credits VS Code logged locally that month, are
  code review and other cloud use. A row with no repository is attributed by
  the repository's share of local VS Code credits (or requests) that month,
  and not at all in a month with no local use.
- Premium requests: "Code Review model" requests are review, "Coding Agent
  model" requests the agent, the rest local chat.

The billing estimate is the headline wherever a month has billing rows of
that kind; elsewhere the run-time estimate is. Both are kept for comparison.
"""

import json
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import carbon_common as cc
import carbon_gha as gha

AGENT_PATH = "dynamic/copilot-swe-agent/"
REVIEW_PATH = "copilot-pull-request-reviewer"

# Assumptions, (low, mid, high) where a range applies. Tokens per agent-minute
# are calibrated on local VS Code agent-mode requests (Sept 2026: 216
# requests, 1654 min): 21,000 prompt tokens (final-call prompts) and 640
# output tokens per minute; summing over each request's model calls instead
# gives about 117,000 prompt tokens per minute, the high bound. Costed as
# uncached input on Claude Sonnet factors, like the VS Code mid estimate.
# credits_per_mtok converts AI Credits to tokens on the same footing: it is
# measured from the local VS Code logs (credits per final-prompt-plus-output
# token) when they have credits, else the fallback (578 per million, i.e.
# $5.78 at $0.01 a credit, the Sept 2026 measurement); its range is a factor
# of two either way, giving the opposite range in tokens. A premium request
# is costed as one median VS Code request.
PARAMS = {
    "agent_tokens_per_min": {"prompt": (10_500, 21_000, 117_000), "output": (320, 640, 1_300)},
    "runtime_family": ("sonnet", "sonnet", "sonnet"),
    "billing_family": ("haiku", "sonnet", "opus"),
    "credits_per_mtok_fallback": 578.0,
    "credits_range": 2.0,
    "output_fraction_fallback": 0.027,
    "request_tokens_fallback": {"prompt": 98_000, "output": 2_700},
    "untracked_share": 0.0,
}


def kind_of(path):
    if path.startswith(AGENT_PATH):
        return "cloud agent"
    if REVIEW_PATH in path:
        return "code review"
    return None


def collect_runs(slug, known=None, workers=8):
    """{run key: record} for Copilot agent and review runs, with durations."""
    known = known or {}
    records = {}
    for wid, (name, path) in gha.workflows(slug).items():
        kind = kind_of(path)
        if not kind:
            continue
        for run in gha.list_runs(slug, wid) or []:
            if run.get("status") != "completed":
                continue
            records[f"{slug}#{run['id']}"] = {
                "repo": slug, "run_id": run["id"], "workflow": name, "kind": kind,
                "title": run.get("title", ""),
                "day": cc.iso_day(run.get("run_started_at") or run.get("created_at")),
                "head_branch": run.get("head_branch") or "", "prs": run.get("prs") or [],
                "pr": gha.pr_number(run), "conclusion": run.get("conclusion") or "",
                "wall_s": gha.seconds(run.get("run_started_at"), run.get("updated_at")),
            }
    todo = [k for k in records if "duration_ms" not in known.get(k, {})]

    def timing(key):
        t = cc.gh_json(f"repos/{slug}/actions/runs/{records[key]['run_id']}/timing")
        return (t or {}).get("run_duration_ms")

    with ThreadPoolExecutor(workers) as pool:
        for key, ms in zip(todo, pool.map(timing, todo)):
            if ms is not None:
                records[key]["duration_ms"] = ms
    return records


def collect_billing(user):
    """Billing rows and per-model premium requests; ({}, {}) without the user scope."""
    usage = cc.gh_json(f"/users/{user}/settings/billing/usage", quiet=True)
    if usage is None:
        cc.warn("billing usage unavailable (needs `gh auth refresh -h github.com -s user`);"
                " Copilot estimated from run time only")
        return {}, {}
    rows, months = {}, set()
    for u in usage.get("usageItems", []):
        if u.get("product", "").lower() != "copilot":
            continue
        day = cc.iso_day(u["date"])
        rows[f"{day}|{u['sku']}|{u.get('repositoryName', '')}"] = {
            "day": day, "month": day[:7], "sku": u["sku"], "unit": u.get("unitType", ""),
            "quantity": u.get("quantity", 0.0), "price": u.get("pricePerUnit", 0.0),
            "repo": u.get("repositoryName", ""),
        }
        if u.get("unitType", "").lower() == "requests":
            months.add(day[:7])
    premium = {}
    for month in sorted(months):
        year, mon = month.split("-")
        rep = cc.gh_json(f"/users/{user}/settings/billing/premium_request/usage"
                         f"?year={year}&month={int(mon)}", quiet=True)
        for u in (rep or {}).get("usageItems", []):
            premium[f"{month}|{u.get('model', '')}|{u.get('sku', '')}"] = {
                "month": month, "model": u.get("model", ""), "sku": u.get("sku", ""),
                "requests": u.get("grossQuantity", 0.0)}
    return rows, premium


def collect_agent_prs(owner):
    """{repo#number: record} for PRs the coding agent opened in owner's repositories."""
    out = cc.run(["gh", "search", "prs", "--author", "app/copilot-swe-agent", "--owner", owner,
                  "--limit", "1000", "--json", "repository,number,createdAt,title"])
    if out is None:
        return {}
    return {f"{p['repository']['nameWithOwner']}#{p['number']}": {
        "repo": p["repository"]["nameWithOwner"], "number": p["number"],
        "month": p["createdAt"][:7], "title": p.get("title", "")} for p in json.loads(out)}


def collect(slug, user, known=None):
    known = known or {}
    rows, premium = collect_billing(user)
    return {"copilot_runs": collect_runs(slug, known.get("copilot_runs")),
            "copilot_billing": rows, "copilot_premium": premium,
            "copilot_agent_prs": collect_agent_prs(slug.split("/")[0])}


def calibration(vscode_months, vscode_records, params=PARAMS):
    """Credits per million tokens, output fraction and median request tokens from VS Code."""
    credits = sum(m["credits"] for m in vscode_months.values() if m["credits"] > 0)
    tokens = sum(m["prompt"] + m["output"] for m in vscode_months.values() if m["credits"] > 0)
    out = sum(m["output"] for m in vscode_months.values() if m["credits"] > 0)
    counted = [r for r in vscode_records.values() if r.get("has_tokens")]
    return {
        "credits_per_mtok": credits / tokens * 1e6 if tokens else params["credits_per_mtok_fallback"],
        "output_fraction": out / tokens if tokens else params["output_fraction_fallback"],
        "request_tokens": ({"prompt": statistics.median(r["prompt"] for r in counted),
                            "output": statistics.median(r["output"] for r in counted)}
                           if counted else params["request_tokens_fallback"]),
        "measured": bool(tokens),
    }


def runtime_cost(minutes, params=PARAMS):
    rate = params["agent_tokens_per_min"]
    kwh, kg = [], []
    for i, fam in enumerate(params["runtime_family"]):
        e = cc.llm_cost(fam, {"input": minutes * rate["prompt"][i],
                              "output": minutes * rate["output"][i]})
        kwh.append(e[0])
        kg.append(e[1])
    return kwh, kg


def tokens_cost(prompt, output, families):
    """(low, mid, high) for three (prompt, output) token pairs on three families."""
    kwh, kg = [], []
    for (p, o), fam in zip(zip(prompt, output), families):
        e = cc.llm_cost(fam, {"input": p, "output": o})
        kwh.append(e[0])
        kg.append(e[1])
    return kwh, kg


def attribute_month(month, slug, billing, premium, agent_prs, runs, vs_months, cal, params):
    """Billing-based (kwh, kg) triples for one month, by kind, attributed to slug.

    Returns {kind: (kwh, kg, note)} for the kinds the month's billing covers.
    """
    repo = slug.split("/")[1]
    vs_all = [m for m in vs_months.values() if m["month"] == month]
    vs_repo = [m for m in vs_all if m["repo"]]

    def local_share():
        for key in ("credits", "requests"):
            total = sum(m[key] for m in vs_all)
            if total:
                return sum(m[key] for m in vs_repo) / total, f"local VS Code {key} share"
        return params["untracked_share"], "no local use this month"

    def agent_share():
        prs = [p for p in agent_prs.values() if p["month"] == month]
        if prs:
            return sum(p["repo"] == slug for p in prs) / len(prs), "agent PR share"
        has_runs = any(r["kind"] == "cloud agent" and r["day"][:7] == month for r in runs.values())
        return (1.0 if has_runs else params["untracked_share"]), "agent runs in repository"

    def share(row, kind):
        if row["repo"]:
            return (1.0 if row["repo"] == repo else 0.0), "repository-attributed row"
        return agent_share() if kind == "cloud agent" else local_share()

    rows = [r for r in billing.values() if r["month"] == month]
    out = {}
    credits = defaultdict(float)
    notes = defaultdict(set)
    requests = defaultdict(float)
    for row in rows:
        if row["unit"].lower() == "aicredits":
            kind = "cloud agent" if "agent" in row["sku"].lower() else "code review"
            qty = row["quantity"]
            if kind == "code review":
                local = sum(m["credits"] for m in (vs_repo if row["repo"] else vs_all))
                qty = max(0.0, qty - local)
            s, why = share(row, kind)
            credits[kind] += qty * s
            notes[kind].add(why)
    by_model = [p for p in premium.values() if p["month"] == month]
    if by_model:
        for p in by_model:
            kind = ("code review" if p["model"] == "Code Review model"
                    else "cloud agent" if p["model"] == "Coding Agent model" else None)
            if kind:
                s, why = share({"repo": ""}, kind)
                requests[kind] += p["requests"] * s
                notes[kind].add(why)
    else:
        for row in rows:
            if row["unit"].lower() != "requests":
                continue
            kind = "cloud agent" if "agent" in row["sku"].lower() else "code review"
            qty = row["quantity"]
            if kind == "code review":
                qty = max(0.0, qty - sum(m["requests"] for m in vs_all))
            s, why = share(row, kind)
            requests[kind] += qty * s
            notes[kind].add(why)
    rng = params["credits_range"]
    for kind, c in credits.items():
        per = cal["credits_per_mtok"]
        total = [c / (per * rng) * 1e6, c / per * 1e6, c / (per / rng) * 1e6]
        f = cal["output_fraction"]
        kwh, kg = tokens_cost([t * (1 - f) for t in total], [t * f for t in total],
                              params["billing_family"])
        out[kind] = (kwh, kg, f"{c:,.0f} AI Credits; " + ", ".join(sorted(notes[kind])))
    for kind, n in requests.items():
        if kind == "cloud agent":
            # One premium request is a whole agent session; run time is better.
            continue
        t = cal["request_tokens"]
        kwh, kg = tokens_cost([n * t["prompt"]] * 3, [n * t["output"]] * 3, params["billing_family"])
        k0 = out.get(kind)
        note = f"{n:,.1f} premium requests; " + ", ".join(sorted(notes[kind]))
        if k0:
            kwh = [a + b for a, b in zip(k0[0], kwh)]
            kg = [a + b for a, b in zip(k0[1], kg)]
            note = k0[2] + "; " + note
        out[kind] = (kwh, kg, note)
    return out


def cost(sources, slug, params=PARAMS, runner_params=gha.PARAMS):
    """Costed items for Copilot runs, and a month-by-month cross-check table.

    Each run gets its run-time estimate. Where the month has billing for that
    kind, the billing estimate replaces it, shared across that month's runs
    of the kind in proportion to duration (one unattributed item if there
    were no runs). Runner energy is added per run.
    """
    runs = {k: r for k, r in sources.get("copilot_runs", {}).items() if r.get("repo") == slug}
    billing = sources.get("copilot_billing", {})
    premium = sources.get("copilot_premium", {})
    agent_prs = sources.get("copilot_agent_prs", {})
    vs_months = sources.get("vscode_months", {})
    cal = calibration(vs_months, sources.get("vscode_copilot", {}), params)
    minutes = {k: (r.get("duration_ms") or r.get("wall_s", 0) * 1000) / 60000 for k, r in runs.items()}
    months = sorted({r["day"][:7] for r in runs.values()} | {b["month"] for b in billing.values()})
    items, check = [], []
    for month in months:
        billed = attribute_month(month, slug, billing, premium, agent_prs, runs, vs_months, cal, params)
        for kind in ("cloud agent", "code review"):
            these = [k for k in runs if runs[k]["kind"] == kind and runs[k]["day"][:7] == month]
            rt = runtime_cost(sum(minutes[k] for k in these), params)
            b = billed.get(kind)
            if these or (b and b[1][1] > 0):
                check.append({"month": month, "kind": kind, "runs": len(these),
                              "minutes": sum(minutes[k] for k in these),
                              "runtime_kg": rt[1], "billing_kg": b[1] if b else None,
                              "billing_note": b[2] if b else "",
                              "headline": "billing" if b else "run time"})
            total_min = sum(minutes[k] for k in these)
            for k in these:
                r = runs[k]
                if b:
                    w = minutes[k] / total_min if total_min else 1 / len(these)
                    kwh, kg = [x * w for x in b[0]], [x * w for x in b[1]]
                else:
                    kwh, kg = runtime_cost(minutes[k], params)
                run_kwh = [gha.runner_kwh({"ubuntu": minutes[k] * 60}, u, runner_params["pue"])
                           for u in runner_params["u_cpu"]]
                items.append({
                    "source": "copilot", "id": k, "day": r["day"], "label": r["workflow"],
                    "kind": kind, "branch": r["head_branch"], "pr": r.get("pr"), "class": "dev",
                    "basis": "billing" if b else "run time", "minutes": minutes[k],
                    "kwh": [a + c for a, c in zip(kwh, run_kwh)],
                    "kg": [a + c * runner_params["grid_kg_per_kwh"] for a, c in zip(kg, run_kwh)],
                })
            if b and not these and b[0][2] > 0:
                items.append({
                    "source": "copilot", "id": f"{slug}|{month}|{kind}", "day": f"{month}-01",
                    "label": kind, "kind": kind, "branch": None, "pr": None, "class": "dev",
                    "basis": "billing", "minutes": 0.0, "kwh": list(b[0]), "kg": list(b[1]),
                })
    return items, check, cal

