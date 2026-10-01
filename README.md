# claude-carbon

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
git clone https://github.com/benjaminpope/claude-carbon.git && cd claude-carbon
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
- **Keeps** per-session, per-day, per-model token totals in
  `~/.claude/carbon/history.json`. Claude Code deletes transcripts after
  `cleanupPeriodDays` (30 by default); the history keeps those days counted
  afterwards. Back this file up if the record matters to you.
- **Sends nothing anywhere.** Everything stays on your machine.

## Command line

```bash
~/.claude/carbon/token_carbon.py                       # totals
~/.claude/carbon/token_carbon.py --by project          # also: day, model, session
~/.claude/carbon/token_carbon.py --since 2026-09-01 --json
```

## Emission factors

From [TokenClimate](https://tokenclimate.com/en/models/claude-opus)
(`tokenclimate-v3-2026-09`, Claude Opus, medium confidence):

| | Energy | CO₂e |
|---|---|---|
| per million input tokens | 238 Wh | 90 g |
| per million output tokens | 5.1 kWh | 1.9 kg |

TokenClimate publishes no factors for cache traffic. The tool assumes:

- cache write = 1.0 × input (the same prefill compute, plus a cache store);
- cache read = 0.1 × input (mirrors the price ratio; a proxy, not a measurement).

**The cache-read assumption dominates the total.** At 0.1× cache reads are
about half the CO₂e; at 1.0× they are over 90%. The dashboard has sliders to
explore this, and the defaults live at the top of `token_carbon.py`
(`CACHE_READ_SCALE`, `CACHE_WRITE_SCALE`). Opus factors are applied to every
model, which overstates Sonnet and Haiku usage.

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

## Limitations

- Covers one machine. Sessions on other machines or in the cloud are not
  included; each person runs their own copy.
- Usage from before installation is included only if its transcripts still
  exist when the tool first runs.
- Estimates depend entirely on third-party factors and the cache assumptions
  above.

## Development

```bash
python3 -m unittest -v
```

## Uninstall

Remove the Stop hook entry that mentions `token_carbon.py` from
`~/.claude/settings.json`, then delete `~/.claude/carbon/` (this deletes your
history).
