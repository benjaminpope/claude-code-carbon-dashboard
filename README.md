# claude-code-carbon-dashboard

Estimates the energy use and CO₂e of your Claude Code usage, split by token
type, and shows it as a local dashboard that refreshes after every turn.

Claude Code's usage panel reports total tokens. This tool separates them into
four types, because each has a very different carbon cost:

| Type | What it is | Typical share of tokens |
|---|---|---|
| Output | tokens the model generates | under 1% |
| Cache read | context re-read from the prompt cache on each call | ~98% |
| Cache write | new context written to the cache | ~1–2% |
| Uncached input | input processed without caching | ~0% |

Output tokens are a tiny fraction of the count but a large share of the
carbon; cache reads are the reverse.

## Install

Requires Python 3.9+ and macOS or Linux. No other dependencies.

```bash
git clone https://github.com/benjaminpope/claude-code-carbon-dashboard.git
cd claude-code-carbon-dashboard
./install.sh
open ~/.claude/carbon/dashboard.html
```

`install.sh` copies the tool into `~/.claude/carbon/`, adds a Stop hook to
`~/.claude/settings.json` (backing the file up to `settings.json.bak` first)
and builds the dashboard once. The hook runs in the background after every
Claude Code turn. Start a new Claude Code session after installing so the hook
loads. Re-run `./install.sh` after pulling changes.

## What it reads and keeps

- **Reads** the transcripts Claude Code keeps in `~/.claude/projects/`,
  including subagent transcripts. It uses only the `usage` block of each API
  call and never reads or stores conversation text.
- **Keeps** per-session, per-day, per-model, per-branch token totals (with
  the working directory) in `~/.claude/carbon/history.json`. Claude Code deletes transcripts after
  `cleanupPeriodDays` (30 by default); the history keeps those days counted
  afterwards. Back this file up if the record matters to you.
- **Sends nothing anywhere.** Everything stays on your machine.

## Command line

```bash
~/.claude/carbon/token_carbon.py                       # totals
~/.claude/carbon/token_carbon.py --by project          # also: day, model, session, family, branch
~/.claude/carbon/token_carbon.py --since 2026-09-01 --until 2026-09-30 --json
```

`--by branch` groups by the git branch Claude Code recorded for each call.
`--json` also lists the underlying records, with their branch and working
directory.

Histories written before branches were recorded are migrated on the first
run: each old session/day/model record becomes branch `?` and keeps its old
counts as a floor. While its transcript still exists the counts move to
their real branches and the `?` record empties, so totals do not change;
once the transcript is gone the old counts stay under `?`.

## Emission factors

Each API call is costed with the factors for its own model family, following
the [TokenClimate methodology](https://tokenclimate.com/en/methodology)
(`tokenclimate-v3-2026-09`). Server energy per million tokens:

| Family | Input | Output | Confidence |
|---|---:|---:|---|
| Claude Haiku | 61 Wh | 1,262 Wh | medium (0.5 × Sonnet) |
| Claude Sonnet | 119 Wh | 2,525 Wh | high (fitted to a published estimate) |
| Claude Opus | 238 Wh | 5,050 Wh | medium (2 × Sonnet) |
| Claude Fable | 476 Wh | 10,100 Wh | low (2 × Opus, price proxy) |

- Cache write = input energy; cache read = 0.08 × input energy
  (TokenClimate's "cache energy" factor, plausible range 0.05–0.20).
- CO₂e = energy × 0.37618 g/Wh: datacentre overhead (PUE 1.14) × grid
  intensity (0.287 kg/kWh), plus 49 g/kWh of amortized hardware carbon.
- The family is read from the model name (`claude-sonnet-5-5` → Sonnet).
  Models that match no family are counted as Opus, and the command line lists
  them.

These reproduce TokenClimate's model sheets (e.g. Opus: 90 g per million input
tokens, 1.9 kg per million output) and the worked example in its methodology;
the tests check both.

**The cache-read factor matters most.** Cache reads are about 98% of tokens,
so at 0.08 they are about half the CO₂e, and at 1.0 they would be over 90%.
The dashboard has sliders to explore this; the defaults live at the top of
`token_carbon.py` (`CACHE_READ_SCALE`, `CACHE_WRITE_SCALE`).

## Everyday equivalents

To make the numbers tangible, each day's total (and the overall and today's
totals) is compared with an item in the
[ALPLA CO₂ Comparison Tool](https://www.alpla.com/en/sustainability/co2-comparison-tool),
e.g. 3.1 kg ≈ 0.78 burgers with chips. For variety, the nearest item on a log
scale and its two neighbours in the table below take turns from day to day
(nearest, one below, one above): a given day always shows the same item, and
consecutive days differ. On the dashboard, the overall and today's totals use
today's date, so they rotate too. The factors were read from the
calculator's own script on 2026-10-01 and live in `COMPARISONS` in
`token_carbon.py`:

| Item | kg CO₂e each |
|---|---:|
| Google search | 0.0002 |
| Message sent to ChatGPT | 0.00432 |
| PET bottle cap | 0.0048 |
| 1 l reusable PET bottle | 0.00632 |
| 1 l recycled-PET bottle | 0.0084 |
| 1 l reusable glass bottle | 0.01308 |
| 250 ml HDPE bottle | 0.038 |
| Hour of video streaming | 0.055 |
| 1 l PET bottle | 0.065 |
| 500 ml aluminium can | 0.089 |
| Detergent bottle | 0.165 |
| Coffee cup | 0.4 |
| Portion of spaghetti with tomato sauce | 0.488 |
| Burger with chips | 4 |
| A tree's annual CO₂ uptake | 24.62 |
| Manufactured smartphone | 80 |
| Economy flight Zurich–London | 232 |
| Year of driving an average car | 4,600 |
| Year of an average European's emissions | 7,250 |
| Year of an average American's emissions | 13,800 |

Packaging items are for production in Germany. These are ALPLA's figures, and
ALPLA is a plastic packaging manufacturer, so treat the packaging rows in
particular as one company's numbers rather than an independent reference.

## Development carbon for a repository

`carbon_report.py` extends the accounting from Claude Code to everything
that went into developing one repository, and writes a markdown page and a
badge. It is installed alongside `token_carbon.py`. Sources, each a flat
module:

| Module | Source | Telemetry | Energy model |
|---|---|---|---|
| `token_carbon.py` | Claude Code | tokens by type, from transcripts | TokenClimate |
| `carbon_vscode.py` | VS Code Copilot Chat | prompt and output tokens per request, from chat session logs | TokenClimate, by assumed model class |
| `carbon_copilot.py` | Copilot cloud agent and code review | run time of their Actions runs; billed AI Credits and premium requests | TokenClimate Sonnet factors |
| `carbon_gha.py` | GitHub Actions CI | runner time per job | Green Algorithms |
| `carbon_slurm.py` | Slurm jobs (OzSTAR/NT) | sacct allocation and run time; NT Job Report usage; `submissions.tsv` | Green Algorithms |

Every estimate has a low, mid and high value, and the parameters of each
model are in one table at the top of its module (`PARAMS`, plus
`MODEL_CLASSES` and `RUNNERS`), with their sources and which values are
assumptions.

```bash
~/.claude/carbon/carbon_report.py --config dev_carbon.toml \
    --markdown docs/dev_carbon.md --summary docs/generated/dev_carbon.json --badge
~/.claude/carbon/carbon_report.py --config dev_carbon.toml --offline   # ledger only
```

It needs `gh` (logged in) for the GitHub sources and `ssh` access without a
password prompt for the remote Slurm logs; a source that is unavailable is
skipped with a warning. The billing endpoints need the `user` scope:
`gh auth refresh -h github.com -s user`.

**Ledger.** Raw records go into a JSON ledger
(`~/.claude/carbon/ledger-<repo>.json` by default), keyed by request id,
run id or job id and max-merged like the history, so records survive after
VS Code chat logs, sacct records (about six months on NT) or GitHub's usage
report expire. Costing is done at report time, so changing a parameter
needs no re-fetch.

**Archive.** The ledger holds per-record detail and stays private. To keep a
permanent copy in a repository, `--archive PATH` compacts the records whose
source has expired, or is about to (Claude transcripts after 25 days, sacct
jobs after 150, GitHub runs and billing after 85, VS Code requests whose
session file is gone), into rows summed by day, source, model or workflow,
kind, class and feature. Rows keep a PR number but not its title, and are
max-merged, so totals never drop. A source's day that has any archived row
is then reported from the archive only, and the rest from the ledger, so
nothing is counted twice. Commit the archive; regenerate the rest.

**Config.** TOML (Python 3.11+, or with `tomli`) or the same structure as
JSON:

```toml
repo = "owner/name"                 # GitHub slug
github_user = "owner"               # for the billing endpoints
ledger = "~/.claude/carbon/ledger-name.json"   # optional
since = "2026-01-01"                # optional day range
until = "2026-12-31"
hide_excluded = true                # drop names of non-headline items from all outputs

[retention_days]                    # optional: when --archive takes a source's records
gha = 85

[claude]
projects = ["name"]                 # substrings of project name, folder or cwd

[vscode]
folders = ["~/code/name"]           # workspaces at or under these paths
storage = "~/Library/Application Support/Code/User"   # optional

[gha]
enabled = true                      # any source can be switched off

[slurm]
host = "nt"                         # ssh host; omit for local logs only
user = "me"                         # for sacct
start = "2026-01-01"                # sacct start date
local_dirs = ["~/code/name/ozstar"] # searched recursively for *.out
remote_dirs = ["/fred/.../jobs/*/logs"]
include = ["*"]                     # job-name or log-folder globs
exclude = ["gpu-test"]

[classes.science]                   # excluded from the headline, listed separately
jobs = ["analysis_*"]
dirs = ["*/analysis/*"]
branches = ["analysis-*"]
sessions_file = "~/notes/science-sessions.txt"  # Claude session ids, one per line

[classes.validation]                # counted, shown as validation
jobs = ["sbc*"]

[features]                          # feature label overrides
"some-branch" = "Label"
"job:bench_*" = "Benchmarks"

[params.slurm]                      # override any module PARAMS entry
pue = 1.4
```

`sessions_file` names a plain-text file with one Claude session id per line;
`#` starts a comment and blank lines are ignored. A listed session takes that
class whatever its branch. A missing file only warns. The ids and comments are
never written to any output.

Unclassified items count as development. Items are attributed to features
through pull requests: by branch (`gh pr list`), by the commit a job pinned
in `submissions.tsv` (`gh api repos/<slug>/commits/<sha>/pulls`), or by the
PR a Copilot run served, and labelled with the PR title. Work on `main` is
"main / unattributed".

**How each source is read and costed**

- *VS Code Copilot Chat.* The chat logs are operation logs (a snapshot, then
  set and append patches), so they are replayed rather than searched; token
  counts are set several times as a response streams. Requests are
  deduplicated on request id, and undone requests still count. The model
  actually used (requests say `copilot/auto`) is read from the result
  metadata. Claude models use their TokenClimate family; GPT and other
  models are given an assumed class with a range. Prompts are costed as
  uncached input (`cached_fraction` sets a cached share); the high value
  counts the prompt of every model call in an agent request. Older requests
  without counts take the median of counted ones.
- *Copilot cloud.* AI Credits are token-metered, so they are converted to
  tokens at the credits-per-token rate measured on the local chat logs. Local
  chat credits are subtracted (VS Code counts them), and account-wide rows
  are attributed by the repository's share of local use or of agent PRs.
  Where a month has no billing, run time times a token rate (also measured
  locally) is used; the report shows both, month by month.
- *GitHub Actions.* The sum of job durations of each finished run, on a
  4-vCPU, 16 GB runner, at an assumed PUE and the US average grid. A run
  with no jobs (cancelled or failed before a runner started) counts zero.
- *Slurm.* Green Algorithms with the allocation from sacct and measured CPU
  and GPU usage from the NT Job Report, NT's EPYC 7543 and A100 power, and
  Victoria's grid factor from the National Greenhouse Accounts Factors 2026.
  A GPU job without a Job Report takes the median measured GPU usage of jobs
  with the same name; jobs still running or pending are left until they end.

References: [TokenClimate methodology](https://tokenclimate.com/en/methodology);
Lannelongue, Grealey & Inouye (2021), [Green Algorithms](https://doi.org/10.1002/advs.202100707),
*Advanced Science* 8, 2100707;
DCCEEW (2026), [Australian National Greenhouse Accounts Factors](https://www.dcceew.gov.au/climate-change/publications/national-greenhouse-accounts-factors).

## Related

[TokenClimate](https://tokenclimate.com), whose factors this uses, also
publishes an open-source Claude Code plugin called `claude-carbon` and a
hosted dashboard for organization-wide usage. This project is independent of
both: a local, single-machine dashboard with a persistent history.

## Limitations

- Covers one machine. Sessions on other machines or in the cloud are not
  included; each person runs their own copy.
- Copilot's cloud inference and non-Claude models' energy are inferred, not
  measured, and carry wide ranges.
- Usage from before installation is included only if its transcripts still
  exist when the tool first runs.
- Estimates depend entirely on third-party factors and the cache assumptions
  above.

## Development

```bash
python3 -m pytest        # or: python3 -m unittest -v
```

## Uninstall

Remove the Stop hook entry that mentions `token_carbon.py` from
`~/.claude/settings.json`, then delete `~/.claude/carbon/` (this deletes your
history).
