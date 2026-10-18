#!/usr/bin/env python3
import argparse
import csv
import json
import sys
from collections import Counter

NULLS = {"", "na", "n/a", "null", "nan", "none"}


def to_number(value):
    try:
        return float(value)
    except ValueError:
        return None


def profile(rows, header):
    columns = {name: [] for name in header}
    for row in rows:
        for name, value in zip(header, row):
            columns[name].append(value.strip())
    result = {}
    for name in header:
        values = columns[name]
        present = [v for v in values if v.lower() not in NULLS]
        numbers = [to_number(v) for v in present]
        stats = {
            "count": len(present),
            "nulls": len(values) - len(present),
            "top": Counter(present).most_common(5),
        }
        if present and all(n is not None for n in numbers):
            stats.update(min=min(numbers), max=max(numbers), mean=sum(numbers) / len(numbers))
        result[name] = stats
    return result


def main():
    ap = argparse.ArgumentParser(description="Per-column profile of a CSV file")
    ap.add_argument("path")
    ap.add_argument("--delimiter", default=",")
    args = ap.parse_args()
    with open(args.path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=args.delimiter)
        header = next(reader, None)
        if header is None:
            print("{}")
            return 0
        json.dump(profile(reader, header), sys.stdout, indent=2, sort_keys=True)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
