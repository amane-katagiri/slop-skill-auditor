# script-tool

Gives the agent a deterministic CSV profiler (`scripts/csv_profile.py`, Python standard library only).
For each column it reports the non-null count, null count, min/max/mean for fully numeric columns and the
five most frequent values. The agent then summarises that JSON instead of guessing from a sample of rows.
