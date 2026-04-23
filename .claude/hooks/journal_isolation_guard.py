#!/usr/bin/env python3
"""
journal_isolation_guard.py
2026-04-23 user-approved. Journal area isolation (CLAUDE.md rule #7).

Design:
  Pollution risk is read-path. Read/Grep/Glob/Bash-read-cmd → BLOCK.
  Write/Edit/Bash-write-cmd → WARN only (setup allowed).

Bypass: env JOURNAL_ISOLATION_BYPASS=1
"""
import json
import sys
import os
import datetime
import re

LOG = '/Users/yuusakuichio/trading/data/logs/journal_isolation_violations.log'
SENTINEL = 'sora' + '_journal'  # split to avoid self-match
JPATH = '/.journal/'
READ_TOOLS = {'Read', 'Grep', 'Glob'}
READ_CMDS = re.compile(r'\b(cat|head|tail|less|more|grep|rg|awk|sed|jq|find)\b')
PERSONAL_KW = re.compile(r'(本音|3:30起床|免疫|コルチゾール|楓ちゃん)')


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    ts = datetime.datetime.now().isoformat(timespec='seconds')
    with open(LOG, 'a') as f:
        f.write(f'{ts} {msg}\n')


def main():
    if os.environ.get('JOURNAL_ISOLATION_BYPASS') == '1':
        return 0

    try:
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    tool = d.get('tool_name', '')
    tinp = d.get('tool_input', {}) or {}

    target_keys = ['file_path', 'path', 'pattern', 'command']
    target_text = ' '.join(str(tinp.get(k, '')) for k in target_keys)

    touches_journal = SENTINEL in target_text or JPATH in target_text

    if touches_journal:
        if tool in READ_TOOLS:
            sys.stderr.write(f'[JOURNAL ISOLATION GUARD] BLOCK: {tool} on journal area. Use separate session (cd ~/{SENTINEL}/ && claude).\n')
            log(f'BLOCK tool={tool} keys={target_text[:200]}')
            return 2

        if tool == 'Bash':
            cmd = str(tinp.get('command', ''))
            if READ_CMDS.search(cmd) and (SENTINEL in cmd or JPATH in cmd):
                sys.stderr.write('[JOURNAL ISOLATION GUARD] BLOCK: Bash read-cmd on journal area.\n')
                log(f'BLOCK tool=Bash cmd={cmd[:200]}')
                return 2

        sys.stderr.write(f'[JOURNAL ISOLATION GUARD] WARN: {tool} touches journal area. Allowed for setup.\n')
        log(f'WARN tool={tool} keys={target_text[:200]}')
        return 0

    combined = str(tinp.get('command', '')) + ' ' + str(tinp.get('pattern', ''))
    if '.jsonl' in combined and PERSONAL_KW.search(combined):
        sys.stderr.write('[JOURNAL ISOLATION GUARD] WARN: jsonl + personal keyword search. Do not use for project decisions.\n')
        log(f'WARN jsonl+kw tool={tool}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
