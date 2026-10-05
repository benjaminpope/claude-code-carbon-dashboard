"""Slurm jobs on OzSTAR's Ngarrgu Tindebeek (NT): run time, allocation and usage.

Three sources, joined on the raw job id (an array task has its own):

- The "Job Report" box NT appends to each job's .out file:

      +------------------ Job Report: 17938486 (COMPLETED) ------------------+
      | Memory (RAM)  [####                ] 20.2% (1.6 GB peak / 8 GB)      |
      | CPU           [######              ] 33.0% average                   |
      | GPU           [####                ] 24.4% average                   |
      | Time          [------>             ] 34.1% (0-00:10:14 / 0-00:30:00) |

  Any line may read "No data available"; CPU-only jobs have no GPU line. It
  gives measured CPU and GPU usage. Log files are read from local folders
  (results pulled back from the cluster) and, over ssh, from remote ones.
- `sacct -P`: elapsed time, allocated CPUs, GPUs and memory (AllocTRES)
  and total CPU time. NT keeps about six months. Steps are listed too
  (without -X) because with -X NT reports TotalCPU as 0; only the
  allocation rows are kept, and theirs is the sum over steps.
- submissions.tsv ledgers (jobid, sbatch, array range, lib@sha, vars,
  submit time), which pin the library commit a job ran.

Energy follows Green Algorithms (Lannelongue, Grealey & Inouye 2021):
t x (n_cpu x P_core x u_cpu + n_gpu x P_gpu x u_gpu + mem x 0.3725 W/GB) x PUE.
"""

import fnmatch
import re
import statistics
from datetime import datetime
from pathlib import Path

import carbon_common as cc

# NT hardware and site parameters. P_core is the AMD EPYC 7543 TDP over its
# cores (225 W / 32); P_gpu the NVIDIA A100-SXM4-80GB TDP (400 W). Memory is
# the allocation, as in Green Algorithms. Where the Job Report has no
# measured usage, u_cpu is TotalCPU / (elapsed x cores) from sacct, and
# failing that, like u_gpu, takes the (low, mid, high) default below; Green
# Algorithms uses 1.0 when usage is unknown. Swinburne publishes no PUE, so
# it takes Green Algorithms' world-average default, 1.67. The grid factor is
# Victoria's location-based scope 2 factor, 0.74 kg CO2e/kWh, from the
# Australian National Greenhouse Accounts Factors 2026 (DCCEEW), Table 1;
# adding the scope 3 (transmission loss) factor would make it 0.85.
# A GPU job without a measured GPU usage takes, as its mid value, the median
# measured usage of jobs with the same name (when at least gpu_peers_min have
# one), widening the default's low and high to include it. Unfinished jobs are not
# costed: sacct's elapsed time is a snapshot and they have no Job Report.
PARAMS = {
    "p_core_w": 225 / 32, "p_gpu_w": 400.0,
    "u_cpu_default": (0.5, 1.0, 1.0), "u_gpu_default": (0.25, 1.0, 1.0),
    "n_cpu_default": 4, "pue": 1.67, "grid_kg_per_kwh": 0.74, "gpu_peers_min": 3,
}

UNFINISHED = {"RUNNING", "PENDING", "REQUEUED", "RESIZING", "SUSPENDED", "CONFIGURING",
              "COMPLETING"}

HEADER = re.compile(r"Job Report: (\d+) \(([A-Z_]+)\)")
MEMORY = re.compile(r"Memory \(RAM\)\s+\[[^\]]*\]\s+([\d.]+)% \(([\d.]+) (\w+) peak / ([\d.]+) (\w+)\)")
USAGE = re.compile(r"\|\s+(CPU|GPU)\s+\[[^\]]*\]\s+([\d.]+)% average")
TIME = re.compile(r"Time\s+\[[^\]]*\]\s+([\d.]+)% \((\S+) / (\S+)\)")
LOG_NAME = re.compile(r"^(.*?)[-_.](\d+)(?:_(\d+))?\.(?:out|err|log)$")
UNITS = {"B": 1e-9, "KB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1e3,
         "K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3}


def duration_s(text):
    """'0-00:10:14', '00:10:14', '10:14' or '14:34.703' -> seconds."""
    if not text or text in ("UNLIMITED", "INVALID"):
        return 0.0
    days, _, rest = text.rpartition("-")
    parts = [float(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_job_reports(text):
    """[{jobid, state, mem_peak_gb, mem_alloc_gb, cpu_pct, gpu_pct, report_elapsed_s, ...}]"""
    reports = []
    for m in HEADER.finditer(text):
        block = text[m.end():].split("\n+-", 1)[0]
        r = {"jobid": m.group(1), "state": m.group(2), "has_gpu_line": "| GPU" in block}
        mem = MEMORY.search(block)
        if mem:
            r["mem_peak_gb"] = float(mem.group(2)) * UNITS.get(mem.group(3), 1.0)
            r["mem_alloc_gb"] = float(mem.group(4)) * UNITS.get(mem.group(5), 1.0)
        for kind, pct in USAGE.findall(block):
            r[f"{kind.lower()}_pct"] = float(pct)
        t = TIME.search(block)
        if t:
            r["report_elapsed_s"] = duration_s(t.group(2))
            r["timelimit_s"] = duration_s(t.group(3))
        reports.append(r)
    return reports


def report_records(text, path):
    """Job Report records for one log file, keyed by raw job id."""
    name = Path(path).name
    m = LOG_NAME.match(name)
    out = {}
    for r in parse_job_reports(text):
        r.update(log=str(path), log_dir=str(Path(path).parent))
        if m:
            r.setdefault("name", m.group(1))
            r["master"] = m.group(2)
        out[r.pop("jobid")] = r
    return out


def tail(path, size=65536):
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - size))
        return fh.read().decode(errors="replace")


def scan_local(patterns):
    """Job Report records from .out files under local folders (globs allowed)."""
    out = {}
    for pattern in patterns:
        base = Path(pattern).expanduser()
        roots = sorted(Path(base.anchor).glob(str(base.relative_to(base.anchor)))) \
            if any(c in pattern for c in "*?[") else [base]
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.out")):
                text = tail(path)
                if "Job Report" in text:
                    recs = report_records(text, path)
                    # sacct's start day wins when it has the job; this is the fallback.
                    day = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
                    for r in recs.values():
                        r["log_day"] = day
                    out.update(recs)
    return out


def scan_remote(host, patterns):
    """Job Report records from .out files on host, read with one grep over ssh."""
    if not patterns:
        return {}
    globs = " ".join(f"{p.rstrip('/')}/*.out" for p in patterns)
    text = cc.ssh(host, f"grep -H -A6 'Job Report' {globs} 2>/dev/null; true")
    if text is None:
        return {}
    by_file = {}
    for line in text.splitlines():
        m = re.match(r"^(.*?\.out)[:-](.*)$", line)
        if m:
            by_file.setdefault(m.group(1), []).append(m.group(2))
    out = {}
    for path, lines in by_file.items():
        recs = report_records("\n".join(lines), path)
        for r in recs.values():
            r["remote"] = host
        out.update(recs)
    return out


def parse_tres(tres):
    """'billing=4,cpu=4,gres/gpu=1,mem=8G,node=1' -> (cpus, gpus, mem GB)."""
    fields = dict(kv.split("=", 1) for kv in tres.split(",") if "=" in kv)
    mem = fields.get("mem", "0")
    m = re.match(r"([\d.]+)([KMGT]?)", mem)
    mem_gb = float(m.group(1)) * UNITS.get(m.group(2) or "M", 1e-3) if m else 0.0
    gpus = sum(int(v) for k, v in fields.items() if k.startswith("gres/gpu") and k.count(":") == 0
               and v.isdigit())
    return int(fields.get("cpu", 0) or 0), gpus, mem_gb


SACCT_FIELDS = "JobID,JobIDRaw,JobName,Elapsed,AllocTRES,TotalCPU,State,Start,End"


def parse_sacct(text):
    out = {}
    for line in text.splitlines():
        cols = line.split("|")
        if len(cols) < 9 or cols[0] == "JobID" or "." in cols[1]:
            continue
        jobid, raw, name, elapsed, tres, totalcpu, state, start, end = cols[:9]
        ncpu, ngpu, mem = parse_tres(tres)
        master, _, task = jobid.partition("_")
        out[raw] = {
            "name": name, "master": master, "task": task.split(".")[0], "elapsed_s": duration_s(elapsed),
            "ncpu": ncpu, "ngpu": ngpu, "mem_alloc_gb": mem, "totalcpu_s": duration_s(totalcpu),
            "state": state.split()[0] if state else "", "start": start, "end": end,
            "day": start[:10] if start[:1].isdigit() else "",
        }
    return out


def sacct(host, user, start):
    text = cc.ssh(host, f"sacct -u {user} -P -n -S {start} -E now --format={SACCT_FIELDS}")
    return parse_sacct(text) if text else {}


def parse_submissions(text, source=""):
    """submissions.tsv rows keyed by job id: sbatch, array range, lib, sha, vars, submit time."""
    out = {}
    for line in text.splitlines():
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 4 or not cols[0].strip().isdigit():
            continue
        lib, _, sha = cols[3].partition("@")
        out[cols[0].strip()] = {"sbatch": cols[1], "array": cols[2], "lib": lib, "sha": sha,
                                "vars": cols[4] if len(cols) > 4 else "",
                                "submitted": cols[5] if len(cols) > 5 else "", "ledger": source}
    return out


def submissions(local, host=None, remote=()):
    out = {}
    for pattern in local:
        base = Path(pattern).expanduser()
        for path in sorted(Path(base.anchor).glob(str(base.relative_to(base.anchor)) + "/**/submissions.tsv")):
            out.update(parse_submissions(path.read_text(errors="replace"), str(path)))
    if host and remote:
        globs = " ".join(f"{p.rstrip('/')}/submissions.tsv" for p in remote)
        text = cc.ssh(host, f"for f in {globs}; do [ -f \"$f\" ] && "
                            f"awk -v f=\"$f\" '{{print f \"\\t\" $0}}' \"$f\"; done; true")
        for line in (text or "").splitlines():
            path, _, row = line.partition("\t")
            out.update(parse_submissions(row, f"{host}:{path}"))
    return out


def collect(local_dirs, host=None, user=None, remote_dirs=(), start="2026-01-01"):
    """{raw job id: record}: Job Reports joined to sacct and the submission ledgers."""
    jobs = {}
    cc.merge_records(jobs, scan_local(local_dirs))
    if host:
        cc.merge_records(jobs, scan_remote(host, remote_dirs))
        if user:
            cc.merge_records(jobs, sacct(host, user, start))
    subs = submissions(local_dirs, host, remote_dirs)
    for r in jobs.values():
        s = subs.get(r.get("master", ""))
        if s:
            r.update({k: v for k, v in s.items() if v})
    return jobs


def usage(measured_pct, default):
    """(low, mid, high) usage: measured if the Job Report has it, else the default."""
    if measured_pct is not None:
        u = measured_pct / 100
        return (u, u, u)
    return default


def job_kwh(r, params=PARAMS, gpu_peer_pct=None):
    """(low, mid, high) kWh for one job record, and whether it is a GPU job.

    gpu_peer_pct, if given, is the mid GPU usage (percent) for a job without
    a measured one: the median of its peers with the same name.
    """
    elapsed = r.get("elapsed_s") or r.get("report_elapsed_s") or 0.0
    ncpu = r.get("ncpu") or params["n_cpu_default"]
    ngpu = r.get("ngpu")
    if ngpu is None:
        ngpu = 1 if r.get("has_gpu_line") else 0
    u_cpu = usage(r.get("cpu_pct"), None)
    if u_cpu is None:
        if r.get("totalcpu_s") and elapsed:
            u = min(1.0, r["totalcpu_s"] / (elapsed * ncpu))
            u_cpu = (u, u, u)
        else:
            u_cpu = params["u_cpu_default"]
    u_gpu = params["u_gpu_default"]
    if r.get("gpu_pct") is None and gpu_peer_pct is not None:
        peer = gpu_peer_pct / 100
        u_gpu = (min(u_gpu[0], peer), peer, max(u_gpu[2], peer))
    u_gpu = usage(r.get("gpu_pct"), u_gpu)
    kwh = [cc.green_algorithms_kwh(elapsed / 3600, n_cpu=ncpu, p_core_w=params["p_core_w"],
                                   u_cpu=u_cpu[i], n_gpu=ngpu, p_gpu_w=params["p_gpu_w"],
                                   u_gpu=u_gpu[i], mem_gb=r.get("mem_alloc_gb") or 0.0,
                                   pue=params["pue"]) for i in range(3)]
    return kwh, ngpu > 0


def cost(records, classes, include=("*",), exclude=(), params=PARAMS):
    """Costed items for jobs whose name or log folder matches include and not exclude."""

    def hit(r, globs):
        return any(fnmatch.fnmatch(r.get("name", ""), g) or fnmatch.fnmatch(r.get("log_dir", ""), g)
                   for g in globs)

    by_name = {}
    for r in records.values():
        if r.get("gpu_pct") is not None:
            by_name.setdefault(r.get("name", ""), []).append(r["gpu_pct"])
    peers = {n: statistics.median(v) for n, v in by_name.items()
             if len(v) >= params.get("gpu_peers_min", 3)}
    items = []
    for jobid, r in sorted(records.items()):
        if not hit(r, include) or hit(r, exclude) or r.get("state") in UNFINISHED:
            continue
        kwh, gpu = job_kwh(r, params, peers.get(r.get("name", "")))
        items.append({
            "source": "slurm", "id": jobid, "day": r.get("day") or r.get("log_day", ""), "label": r.get("name", ""),
            "kind": "GPU job" if gpu else "CPU job", "branch": None, "pr": None,
            "sha": r.get("sha"), "class": cc.classify(classes, job=r.get("name"), dir=r.get("log_dir")),
            "hours": (r.get("elapsed_s") or r.get("report_elapsed_s") or 0) / 3600,
            "kwh": kwh, "kg": [k * params["grid_kg_per_kwh"] for k in kwh],
        })
    return items
