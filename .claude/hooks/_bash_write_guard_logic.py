"""_bash_write_guard_logic.py — CRIT-R6-1 detection logic for bash_write_guard.sh

Reads command from stdin, prints BLOCK or OK.

C-R7-1 fix: 13 bypass vectors added to WRITE_CMD_PATTERNS:
  shutil.copy / pathlib.write_text / os.rename / git apply / patch /
  install -m / ln -sf / tr (overwrite) / python open wb / open a /
  heredoc redirect / dd bs= / open with write modes

C-R7-2 fix: PROTECTED_PATTERNS extended with atlas_v3/ common_v3/ chronos_v3/
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
    # C-R7-2 fix: atlas_v3/ common_v3/ chronos_v3/ 配下も保護対象に追加
    r'atlas_v3/[a-zA-Z_/]',
    r'common_v3/[a-zA-Z_/]',
    r'chronos_v3/[a-zA-Z_/]',
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
    # C-R7-1 fix: 13 bypass vectors
    # shutil.copy / shutil.copyfile / shutil.move / shutil.copytree
    r'shutil\.(copy|copyfile|move|copytree)\s*\(',
    # pathlib write_text / write_bytes
    r'\.write_text\s*\(',
    r'\.write_bytes\s*\(',
    # os.rename / os.replace / os.link / os.symlink
    r'os\.(rename|replace|link|symlink)\s*\(',
    # git apply (patch via git)
    r'git\s+apply\b',
    # patch command
    r'\bpatch\b.*-p',
    r'patch\s+<',
    # install(1) with -m (installs file with mode)
    r'\binstall\s+(-[a-zA-Z]*m|-m)\b',
    # ln -sf (force symlink = can overwrite)
    r'\bln\s+.*-[a-zA-Z]*s[a-zA-Z]*f\b',
    r'\bln\s+.*-[a-zA-Z]*f[a-zA-Z]*s\b',
    # tr with redirect (tr ... > file)
    r'\btr\b.*>',
    # python open with write modes: 'w', 'wb', 'a', 'ab', 'r+'
    r"""open\s*\([^)]*['"](%s)['"]\s*\)""" % 'w|wb|a|ab|r\\+|w\\+',
    # heredoc redirect to file
    r'<<\s*[A-Z_]+.*>',
    # dd with bs= (alternate form)
    r'\bdd\s+bs=',
]

has_protected = any(re.search(p, command) for p in PROTECTED_PATTERNS)
has_write = any(re.search(p, command, re.IGNORECASE) for p in WRITE_CMD_PATTERNS)

if has_protected and has_write:
    print("BLOCK")
else:
    print("OK")
