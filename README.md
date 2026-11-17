# claude-usage

Local stats for Claude Code: cost, calls and tokens per model, read from the transcripts in `~/.claude/projects`. Nothing leaves the machine. Python 3 stdlib only.

Sibling of `../copilot-usage`, same dashboard, different reader. It runs on port 8766 so both can be open at once.

## Usage

```sh
ln -sf "$PWD/claude_usage.py" ~/.local/bin/claude-usage

claude-usage                                  # table per month
claude-usage --by day --since 2026-09-01      # or --by repo, --by branch
claude-usage serve                            # live dashboard at http://localhost:8766
claude-usage serve --budget 500               # also track a monthly USD budget
```

`CLAUDE_CONFIG_DIR` is respected if you keep Claude Code's data somewhere other than `~/.claude`.

## How the numbers are derived

- Transcripts record tokens, not money. Cost is tokens times the API list price from `PRICES` at the top of `claude_usage.py`. Cache writes are 1.25x the input price for the 5 minute cache and 2x for the 1 hour cache, and fast mode doubles everything. On a Pro or Max subscription this is what the usage would have cost on the API, not what you paid.
- Checked against the `cost-state` totals Claude Code writes itself: over 82 sessions the computed total came to 99% of the logged one. Update `PRICES` when prices change, since new models fall through to the Opus rate.
- One API response is written as several transcript lines (one per content block) that repeat the same usage, and resumed or forked sessions copy their history into a new file. Calls are deduplicated by message id, within and across files.
- Subagent calls live in `<session>/subagents/agent-*.jsonl` and are marked `isSidechain`. They count towards the parent session.
- A prompt is a user message that is not a tool result, a slash command, a system notice or meta. It is credited to the first model call that follows it.
- Small background calls (session titles and the like, usually Haiku) are not in the transcripts, so they are missing here. They are a fraction of a percent of spend.
- Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default, in `~/.claude/settings.json`). Raise it if you want "All time" to reach further back.

## Dashboard

- Same layout as copilot-usage: filters in the URL hash, clickable bars, rows and open sessions, sortable tables, previous-window comparison, month-end projection against `--budget`, CSV export, and the theme picker under Settings.
- The cumulative spend card always shows the current month, whatever range is selected (filters still apply). The dashed forecast is the month's daily average carried to month end, the same figure as "On pace for". With `--budget`, a budget line is drawn when it is within 2x of the forecast.
- Model families (Fable, Opus, Sonnet, Haiku) are guessed from model names by `FAMILY_RULES`.
- Input counts fresh, cached and cache-written tokens together, so "input from cache" is the share of the whole prompt that was a cache read. Thinking tokens are part of output.
- The sessions table takes titles from `ai-title` events, turns and active time from `turn_duration`, compactions from `compact_boundary`, PRs from `pr-link` and lines changed from `cost-state`. Those cover the whole session, not the selected range.
- Open sessions come from `~/.claude/sessions/<pid>.json` for pids that are still alive.
- There is no per-call latency in the transcripts, so there is no speed table.
