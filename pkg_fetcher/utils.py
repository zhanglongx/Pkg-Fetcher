from __future__ import annotations

import sys
from dataclasses import dataclass

@dataclass
class ExecResult:
    """Remote command execution result."""
    exit_status: int
    stdout: str
    stderr: str


class ToolError(Exception):
    """Generic tool exception with a human-readable message."""
    pass


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def shlex_quote(s: str) -> str:
    """POSIX-ish single-quote escaping."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def quote_for_shell(cmd: str) -> str:
    """Wrap a command for bash -lc argument."""
    return shlex_quote(cmd)