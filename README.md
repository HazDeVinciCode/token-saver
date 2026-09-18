# TOKEN SAVER

[Version française](README.fr.md) · Python ≥ 3.10, no dependency · 78 automated tests (Windows, macOS, Linux) + end-to-end validations with the real Claude Code · MIT license.

> The tool's own messages are in French for now (an English message set is planned). Everything else — installation, commands, files — works the same in any language, and Claude will talk to you in yours.

## In one sentence

A **per-project** tool for Claude Code that **measures** where your tokens go, **avoids** waste without touching the quality of the work, and runs your autonomous work **one fresh session per cycle** — installed and removed with one command, nothing global on the machine.

## Install (any project)

Once per machine: clone this repository (for example into `~/dev/token-saver`, or `C:\dev\token-saver` on Windows).

Then, inside the project, in Claude Code:

> install TOKEN SAVER from `~/dev/token-saver`

Claude reads `INSTALL.md`, asks one question (mode **Autonomous** / **Light** / **Measure only** / **Cancel**), installs, and tells you what is active. Terminal equivalent, from the project:

```bash
python <repo>/dist/token-saver.pyz install --profile light      # --dry-run: show the plan without writing anything
```

What it creates: a `.token-saver/` folder (ignored by git, with its own copy of the tool), a few lines in `.claude/settings.local.json` (personal, not versioned, backed up first), its own rules file `.claude/rules/token-saver.md` (read by Claude Code like CLAUDE.md), and the `/token-saver…` shortcuts. **No project file is modified**: not CLAUDE.md, not your commands, not your agents. Everything is recorded in a manifest: `/token-saver-off` puts the project back **exactly** as it was.

## What it does, automatically

| | What it does | Effect on quality |
|---|---|---|
| **Meter** (`/token-saver-status`, `/token-saver-dashboard`) | Reads the logs Claude Code already writes; plain-words summary: spent, saved, continuity of the work; HTML dashboard; every number is labelled **measured** or **estimated** | none |
| **Advisor** (`/token-saver-doctor`) | Findings ranked by impact, with the why and the number; includes the **agents advisor**: project agents that no longer serve (their description is re-read on every call), measured cost per launch and heavy agents, candidates for a cheaper model (on Opus without writing code); full table with `token-saver agents` | none (it advises, it changes nothing) |
| **Anti re-read** | Refuses an identical re-read of a large unchanged file; never blocking (a retry always passes) | none |
| **`find`** | Gives Claude the 2-3 useful sections of a large document instead of the whole document | none (full reads stay possible) |
| **Protected summaries** | When Claude Code compacts the session, the summary keeps the procedure in progress and the remaining checks; a factual reminder follows (never an instruction) | protects continuity |
| **Tool pruning** (`/token-saver-prune`) | Removes from the context the tools and plugins **this project has never used** in its history (≈ 10-20K fewer tokens re-read per call in the desktop app); never a core tool; preview first, `--apply` to write, `--undo` to put them back | none (nothing that already served is removed) |
| **Autonomy in fresh sessions** (`/token-saver-autonomie`, or the project's own autonomy command) | Each work cycle in a **fresh session** driven from outside (`claude -p`), no compaction, a 22K startup base instead of 63K | better: each cycle restarts from the state files, no summary loses a step |
| **Handoff** (`/token-saver-next`) | Task done, Claude writes a short state note; the next fresh session receives it by itself at startup: one session per task, nothing lost | better: no more conversation growing for days |
| **Bounded code review** (in the project's review command, else `/token-saver-review`) | The diff is written to a file and read once; the next round re-reads only the changed lines, with the 🔴 to verify; 3 rounds maximum then the PR goes back to a human; only 🔴 block the merge; CI is awaited in a single call | same depth of reading, without repetition |
| **Check** (in `/token-saver-status`) | 20 read-only checks: settings, hooks, integrity, leftovers, setting applied to the open session, no core tool removed, review procedure in place | — |

What it **never** does: change a setting while a session is running, force frequent compactions (measured: it destroys continuity), touch your global configuration, run a background program without your command.

## Autonomy in fresh sessions

The cost of a long autonomous run is structural: a single session grows for hours (290-340K tokens re-read on **every** call, measured) and its summaries lose steps. Autonomy in fresh sessions runs each cycle in a new conversation, driven from outside (`claude -p`):

- Cycle recipe: the project's command if it has one (for example `/autonomie 1`, detected at install). Otherwise, on the first `/token-saver-autonomie`, the tool **looks at what the project contains** (commands, agents, test command, `TODO.md` or GitHub issues, repository, CI, review command), proposes a recipe assembled from that, asks at most two questions (where are the tasks, how to run the tests) and saves it after your approval in `.token-saver/cycle.md`, readable and editable; `/token-saver-autonomie config` revisits it. With nothing in the project, the base recipe is enough: one task, done with tests, bounded review, commit on an `autonomie/<task>` branch, journal and state in `.token-saver/nuit/`, `[nuit:fin]` marker when nothing is left. Nothing is invented: no agent, no command, no task source.
- `/token-saver-autonomie 6` (6 cycles; nothing = until stopped) · `/token-saver-status` (right now, or the report) · `/token-saver-autonomie-stop` (end of the current cycle) or `--now`.
- Before starting, the check verifies: binary found, cycle recipe available, CLI logged in (one-turn test call), **no other active session on the project**. While running: automatic wait and resume after a quota limit, stop after two consecutive failures, log in `state/nuit.log`, per-cycle measures in `metrics/nuit-cycles.jsonl`.
- After `/token-saver-autonomie`, do not work in the app on this project while it runs (two teams on the same code would get in each other's way).
- **Automatic (hook H6)**: the project's autonomy command, detected at install (`/autonomie-totale`, `/autonomie N`…), typed in an app session, launches the fresh sessions instead, and the message is refused with the explanation; `/stop-autonomie` (or `/token-saver-autonomie-stop`) stops them; meanwhile, the app's other messages on this project are refused. If the fresh sessions cannot start (CLI not logged in, another session modifying the project in the last 10 minutes), the message is refused with the reason; typing the same command again within 2 minutes retries, and if it still fails, starts it in the app as you insist. A session that only chats never counts as "active".
- **Follow-up and end**: `/token-saver-status` shows what the cycle is doing **right now** (last action, context, last tools, Claude's last text — read from its log, zero tokens). At the end the tool writes the **autonomy report** itself: cycles, minutes, tokens re-read including sub-agent transcripts, average context, compactions, limits, review rounds, merged PRs if the project is on GitHub else commits, compared with the previous run. The app session learns at startup that an autonomy is running (do not touch the repository) or has just ended (nothing is running).
- **End time**: `nuit.end_at` in the config (or `--until 08:00` on the command line), in addition to `max_hours`; the run finishes its current cycle then stops.
- **Unfinished cycle**: if a cycle ends waiting, failed, with unpushed commits or uncommitted files, the next cycle is told in plain words and resumes that work before starting another.
- **Environment checks**: declared by the project in `nuit.env_checks` (`name`, `command`, `expect` = `exit0` | `nonempty` | `regex:…`, `required`) and proposed at install **from what the project contains** — a GitHub workflow, a GitHub remote and `gh` → CI state; scripts mentioning `adb`/Android and `adb` present → device plugged. Nothing is assumed: a project without CI or device gets no check. They warn at launch (`⚠`) and only block if `required`.

Prerequisite, once per machine: the `claude` CLI must be logged in outside the app (the runner finds the binary on the PATH, or the one embedded by the desktop app on Windows; otherwise set `nuit.claude_cmd` in `.token-saver/config.json`). `/token-saver-status` says so and explains.

Validation: on a test project, 2 real cycles → 2 fresh sessions, **0 compaction**, maximum context 32K, 2 tasks delivered with tests, branches, commits, journal. On a real project, two full nights: 16 then 27 PRs merged, 3 to 4.5 times fewer tokens than the same work in one growing app session, equal or better quality on inspection.

## Code review

Measured on two real projects: up to 8 re-reads of the same PR, 7 to 8M tokens and 15 to 85 minutes per re-read, each round re-reading the whole PR; one round with six specialists in parallel = 116M tokens; 710 calls that only polled the CI state. Reviews weighed 14 % and 22 % of those projects' volume.

At install, TOKEN SAVER finds the project's review command, whatever its name (`/review-pr`, `/code-review`, `/revue`…), without modifying it: when the review starts, whether you type the command or Claude runs it itself inside a cycle, a hook whispers the procedure to it (same text, ≈ 450 tokens, only at that moment; verified with the real Claude Code in both cases). The project's checklist does not change; the procedure sets what is read, the number of rounds and what blocks:

- `review begin` writes the diff to a file: **round 1 = full diff**, **round 2 = the lines changed since round 1** with the 🔴 to verify, **round 3 = verification of the remaining 🔴**. Never a 4th round: the PR goes back to a human.
- The specialists the command plans are called in round 1, then only those who raised a 🔴; each receives the diff file, never the whole PR.
- Report of at most 12 findings, `file:line`, 🔴 / 🟠 / 🟢; `review end` derives the verdict from the report: **only 🔴 block the merge**; 🟠 are fixed in the same PR before the merge or dismissed in writing in its description, **never a follow-up card or issue** (measured on a real project: 36 "follow-up" cards out of 63 open, fed by the previous instruction); a 🟠 describing a defect the user would see is a 🔴.
- `review wait-ci` waits for the CI in a single call, instead of repeated polling at full context.
- Without a review command in the project, `/token-saver-review` provides a generic checklist with the same procedure.
- **Any review delegated to an agent goes through it too** (hook H3): when a process launches an agent to review code, outside the review command, the hook prepares the diff, puts the brief at the top of its instructions (round, diff file, 🔴 to verify, report format) and records its report on return; the verdict is added to the orchestrator's context. Validated with the real Claude: sub-agent review in 35 s, verdict recorded without intervention.

The advisor flags chained re-reads (R17) and CI polling (R18) with their measured cost.

## What to expect, honestly

- Autonomy in fresh sessions: average re-read context divided by ~2 on long processes (ESTIMATED ≈ −40 % tokens on a long autonomous process, to be measured by the meter on each project), and above all no more destructive summaries.
- Bounded review: review tokens divided by 3 to 4 (ESTIMATED: rounds 2 and 3 on the delta only, no 4th round), same depth of reading; check with `/token-saver-doctor` after a few PRs.
- Tool pruning: ≈ 10-20K fewer tokens per call in the desktop app (ESTIMATED from `/context`).
- Anti re-read and `find`: small, measured.
- What the tool will not save: the work the process itself demands (chained re-reads, parallel tasks). The meter shows you where it goes; the decision stays yours.

Measured lessons: forcing frequent compactions (150K-200K) multiplied compactions by 34 and lost steps; a hook that changed a setting mid-session caused a loop. The context right after a summary is already ~110K on a large project; any window too close to that floor is refused. No hook touches a context setting anymore.

## Uninstall

```bash
python .token-saver/bin/token-saver.pyz uninstall            # or /token-saver-off — removes settings, hooks, shortcuts, rules file, pruning; keeps the metrics
python .token-saver/bin/token-saver.pyz uninstall --purge    # also deletes metrics, reports, backups
```

A running autonomy is stopped first. A key you modified yourself in the meantime is kept and reported.

## The four registers

| Register | Content | Source |
|---|---|---|
| **MEASURED** | tokens reported by the API (input, cache read/write, output, thinking), context per call, calls per turn, cold rebuilds, re-reads, compactions, quota rejections | JSONL transcripts |
| **AVOIDED-MEASURED** | tokens an action of the tool kept out of the context, known size | hook logs |
| **ESTIMATED** | counterfactuals and indicative sizes, assumption always shown | computation |
| **OVERHEAD** | tokens injected by the tool, hook latency, processes (0) | hook logs |

Net gain = AVOIDED-MEASURED − OVERHEAD. Estimates are never added to measurements.

## Files

```
.token-saver/
├── config.json            features, thresholds, profile, `find` sources, autonomy settings (`nuit`)
├── manifest.json          exact inventory of the installation (basis of the uninstall)
├── bin/                   token-saver.pyz, launchers, statusline.py
├── state/                 ledgers, pre-compaction snapshots, autonomy state and log, review rounds (diff + report per round)
├── metrics/               usage.sqlite, docs.sqlite, hook-events.jsonl, savings.jsonl, nuit-cycles.jsonl, review-log.jsonl
├── cycle.md               validated cycle recipe (project without a cycle command); recette.json = your answers
├── nuit/                  ETAT.md, JOURNAL.md (TOKEN SAVER recipe)
├── reports/               dashboard.html
└── backups/               settings.local.json.<timestamp>.bak
```

## Troubleshooting

- **A hook wrongly refuses a read**: running exactly the same read again always passes; or `features.H1_read_ledger: false` in `.token-saver/config.json`.
- **`python` not found**: hooks, statusline and runner call `python` from the PATH, or `python3` when only that exists (chosen at install); the installation checks it.
- **The autonomy refuses to start**: `/token-saver-status` says why (CLI login, another active session, missing cycle recipe).
- **Numbers different from `/usage`**: `/usage` covers the main conversation; the tool reads all the project's transcripts (sub-agents included). The exact weighting of the subscription quota is not public: weighted tokens are labelled ESTIMATED.

## Development

```
src/token_saver/   paths, config, db, collect, metrics, status, dashboard(+html), install, hooks, find, doctor, agents, prune, nuit, recette, review, statusline, cli
tests/             unittest, no dependency (fake_claude.py simulates `claude -p`); run on Windows, macOS and Linux by GitHub Actions
```

Build the archive: `python -c "import zipapp; zipapp.create_archive('src','dist/token-saver.pyz',main='token_saver.cli:entry',compressed=True)"`. MIT license.
