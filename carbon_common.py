"""Shared pieces for the collectors: subprocess helpers, the ledger, energy models.

Each collector (carbon_vscode, carbon_copilot, carbon_gha, carbon_slurm)
gathers raw telemetry into records keyed by a source-specific id: a request
id, a workflow run id, a Slurm job id. The records go into a JSON ledger that
is max-merged like token_carbon's history: numbers keep the larger of the
stored and new value, other fields take the newest non-empty value, and
records that a source no longer returns (an expired chat file, a job past
sacct's retention) keep what was stored. Costing happens at report time
from the raw records, so changing a parameter never needs a re-fetch.

Every cost is a (low, mid, high) triple of facility energy in kWh (server
energy times PUE) and kg CO2e.
"""

import fcntl
import fnmatch
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import token_carbon as tc

LEDGER_VERSION = 1

# Green Algorithms (Lannelongue, Grealey & Inouye 2021, Adv. Sci. 8, 2100707):
# power draw of memory, per GB allocated.
GA_MEM_W_PER_GB = 0.3725


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def run(cmd, timeout=120, input=None):
    """stdout of cmd, or None (with a warning) if it is missing, fails or hangs."""
    if not shutil.which(cmd[0]):
        warn(f"{cmd[0]} not found; skipping")
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired:
        warn(f"{' '.join(cmd[:3])} timed out")
        return None
    if p.returncode != 0:
        warn(f"{' '.join(cmd[:3])} failed: {p.stderr.strip()[:200]}")
        return None
    return p.stdout


def gh_json(path, paginate=False, jq=None, quiet=False):
    """`gh api path` parsed as JSON; with jq, a list of one value per output line."""
    cmd = ["gh", "api", path] + (["--paginate"] if paginate else []) + (["--jq", jq] if jq else [])
    if quiet:
        # A 404 here is expected (e.g. a token without the user scope).
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = p.stdout if p.returncode == 0 else None
    else:
        out = run(cmd, timeout=600)
    if out is None:
        return None
    if jq:
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    return json.loads(out) if out.strip() else None


def ssh(host, command, timeout=300):
    """Run a read-only command on host without prompting; None if that fails."""
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, command],
               timeout=timeout)


def iso_day(ts):
    """'2026-10-05T08:11:08Z' or epoch ms -> '2026-10-05' (UTC)."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()
    return str(ts or "")[:10]


def merge_records(old, new):
    """Max-merge new records into old, in place (see the module docstring)."""
    for key, cur in new.items():
        prev = old.get(key)
        if prev is None:
            old[key] = cur
            continue
        for f, v in cur.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                if v not in (None, "", [], {}):
                    prev[f] = v
            else:
                p = prev.get(f)
                prev[f] = max(p, v) if isinstance(p, (int, float)) else v
    return old


def load_ledger(path):
    path = Path(path).expanduser()
    if not path.exists():
        return {"version": LEDGER_VERSION, "sources": {}}
    data = json.loads(path.read_text())
    if data.get("version") != LEDGER_VERSION:
        sys.exit(f"{path}: unknown ledger version {data.get('version')}")
    return data


def update_ledger(path, fresh):
    """Max-merge {source: {id: record}} into the ledger at path and return it."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = load_ledger(path)
        for source, records in fresh.items():
            merge_records(ledger["sources"].setdefault(source, {}), records)
        ledger["updated"] = datetime.now().astimezone().isoformat(timespec="minutes")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True))
        tmp.replace(path)
    return ledger


def classify(classes, **fields):
    """The first class whose globs match any field, else 'dev'.

    classes is {"science": {"jobs": [...], "dirs": [...], "branches": [...]}, ...};
    a field name maps to its glob list by adding "s" (job -> jobs, dir -> dirs,
    branch -> branches). Science is checked first, then validation, then any
    other class in config order.
    """
    order = sorted(classes, key=lambda c: (c != "science", c != "validation"))
    plural = {"branch": "branches"}
    for cls in order:
        for f, value in fields.items():
            globs = classes[cls].get(plural.get(f, f + "s"), [])
            if value and any(fnmatch.fnmatch(value, g) for g in globs):
                return cls
    return "dev"


def triple(lo, mid, hi):
    return [float(lo), float(mid), float(hi)]


def llm_cost(family, tokens, cache_read_scale=None):
    """(kWh, kg) of facility energy for a token dict on a TokenClimate family.

    tokens has any of input, cache_write, cache_read, output. Server energy
    uses token_carbon's factors; kWh includes the cloud PUE and kg uses the
    same CO2 per server Wh as token_carbon.
    """
    wh = 0.0
    for t, n in tokens.items():
        per = tc.wh_per_mtok(family, t)
        if t == "cache_read" and cache_read_scale is not None:
            per = tc.FAMILIES[family]["in_wh"] * cache_read_scale
        wh += n / 1e6 * per
    return wh * tc.PUE / 1000, wh * tc.CO2_G_PER_WH / 1000


def green_algorithms_kwh(hours, n_cpu=0, p_core_w=0.0, u_cpu=1.0, n_gpu=0, p_gpu_w=0.0,
                         u_gpu=1.0, mem_gb=0.0, pue=1.0):
    """Green Algorithms energy: t x (cores x P x u + GPUs x P x u + mem x 0.3725 W) x PUE."""
    watts = n_cpu * p_core_w * u_cpu + n_gpu * p_gpu_w * u_gpu + mem_gb * GA_MEM_W_PER_GB
    return hours * watts * pue / 1000
