#!/usr/bin/env python3
"""Small text metrics for experiment metric commands (stdlib only).

Inside check/metric commands this directory is available as $SLOP_TOOLS:
  python3 -I "$SLOP_TOOLS/text_metrics.py" edit-ratio draft.md out.md
  python3 -I "$SLOP_TOOLS/text_metrics.py" count-absent out.md "planted typo" "duplicated sentence"

edit-ratio A B        normalized character-level edit ratio (difflib.SequenceMatcher):
                      0 = identical, 1 = completely different.
count-absent FILE P.. number of the given phrases that do not occur in FILE
                      (e.g. how many planted problems were removed).
Each command prints one number on the last stdout line.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def edit_ratio(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return 1.0 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def count_absent(text: str, phrases: list[str]) -> int:
    return sum(1 for p in phrases if p not in text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="text_metrics.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("edit-ratio", help="0 = identical, 1 = completely different")
    p.add_argument("a")
    p.add_argument("b")
    p = sub.add_parser("count-absent", help="number of phrases absent from FILE")
    p.add_argument("file")
    p.add_argument("phrases", nargs="+")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "edit-ratio":
            print(f"{edit_ratio(read_text(a.a), read_text(a.b)):.6f}")
        else:
            print(count_absent(read_text(a.file), a.phrases))
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
