---
name: script-tool
description: Profile a CSV file column by column (counts, nulls, numeric min/max/mean, top values). Use when the user asks what is in a CSV or wants a quick data profile.
license: MIT
---

# CSV profiling

Do not eyeball large CSV files. Run the bundled profiler and summarise its JSON output:

```bash
python3 scripts/csv_profile.py <file.csv>
```

The script prints one JSON object per column with:

- `count`: non-empty values
- `nulls`: empty cells or `NA`/`null`/`NaN`
- `min`, `max`, `mean`: only when every non-null value parses as a number
- `top`: the 5 most frequent values with counts

Report surprising columns first (high null ratio, a numeric column with a few text values).
Use `--delimiter ';'` for semicolon-separated files.
