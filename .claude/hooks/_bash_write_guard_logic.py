"""_bash_write_guard_logic.py — CRIT-R6-1 detection logic for bash_write_guard.sh

Reads command from stdin, prints BLOCK or OK.
"""
import sys
import re

command = sys.stdin.read()

PROTECTED_PATTERNS = [
    r'spy_bot\.py',
    r'chronos_bot\.py',
    r'atlas_agent\.py',
    r'chronos_agent\.py',
    r'atlas_watchdog\.py',
    r'chronos_watchdog\.py',
    r'strategy_selector\.py',
    r'symbol_selector\.py',
    r'chronos_strategy_selector\.py',
    r'chronos_pre_trade_check\.py',
    r'chronos_rule_simulator\.py',
    r'tradovate_client\.py',
    r'gmail_monitor\.py',
    r'sora_heartbeat_monitor\.py',
    r'common/[a-zA-Z_]',
]

WRITE_CMD_PATTERNS = [
    # inplace edit
    r'sed\s+-i',
    r'perl\s+-i\b',
    # python open write
    r'python[23]?\s+-c.*open\s*\(',
    r'python[23]?\s+-c.*\.write\(',
    # awk inplace
    r'awk\s+-i\s+inplace',
    # shell redirects
    r'(echo|printf)\s+.*>',
    r'cat\s+>',
    # rsync (can overwrite)
    r'rsync\b',
    # tee (can overwrite)
    r'\btee\b',
    # cp/mv
    r'\bcp\b',
    r'\bmv\b',
    # git restore/checkout
    r'git\s+checkout-index',
    r'git\s+checkout\s+--\s+',
    # truncate/dd
    r'\btruncate\b',
    r'\bdd\b.*of=',
]

has_protected = any(re.search(p, command) for p in PROTECTED_PATTERNS)
has_write = any(re.search(p, command, re.IGNORECASE) for p in WRITE_CMD_PATTERNS)

if has_protected and has_write:
    print("BLOCK")
else:
    print("OK")
