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
  intensity (0.287 kg/kWh), plus 49 g/kWh of amortised hardware carbon.
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

## Related

[TokenClimate](https://tokenclimate.com), whose factors this uses, also
publishes an open-source Claude Code plugin called `claude-carbon` and a
hosted dashboard for organisation-wide usage. This project is independent of
both: a local, single-machine dashboard with a persistent history.

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
