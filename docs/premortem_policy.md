# Premortem Policy (all Sora Lab agents)

Source: ゆうさくさん directive 2026-04-12
Method: Gary Klein premortem + HAZOP guide words (IEC 61882) + ACH (Heuer)

## Why
unittest only covers the expected. Before starting a task we must
systematically enumerate how it can fail.

## Scope (per agent)
- builder: new features, large refactors, VPS production changes
- ops: production restart/deploy/service changes
- strategist: new tactics, large parameter changes
- atlas_agent: before Level 2 (AUTOFIX) or Level 3 (ALERT) dispatch

Small tasks (< 30 min work, no side effects) may skip it.

## How
```
python3 /Users/yuusakuichio/trading/scripts/premortem.py \
  --task "<task description>" --files "<comma-separated files>"
```
Outputs:
- data/premortem_reports/<timestamp>_<slug>.md
- data/premortem_reports/<timestamp>_<slug>.json

## Required fields at top of agent final response
- absolute report path
- overall_risk (low/medium/high/critical)
- go_no_go (GO / CONDITIONAL_GO / NO_GO)
- top3_blockers (3 ids F01..F10)
- required_gates (list)

## Decision behaviour
- GO: proceed
- CONDITIONAL_GO: clear all required_gates before implementation (no bypass)
- NO_GO: stop, send Pushover, wait for owner decision

## Enforcement
`.claude/hooks/premortem_gate.sh` runs as PreToolUse. If an Agent call
has prompt >= 500 chars and no premortem md in data/premortem_reports/
within the last 30 minutes, the hook exits 2 and prints guidance.
Escape: `export PREMORTEM_BYPASS=1` (audit log remains).

Tunables:
- PREMORTEM_WINDOW_MIN (default 30)
- PREMORTEM_PROMPT_THRESHOLD (default 500)

## Atlas integration (to be wired in atlas_agent.py)
Before a Level 2 autofix or Level 3 stop, invoke
scripts/premortem.py and attach the JSON result to
data/logs/atlas_actions.log. If judgment is NO_GO, skip the autofix
and escalate Level +1.

## Verification
- python3 scripts/premortem.py --selftest exits 0
- data/premortem_reports/ gains a new md+json pair
- builder/ops responses start with the premortem summary fields
