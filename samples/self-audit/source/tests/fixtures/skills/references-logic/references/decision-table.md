# Semantic version decision table

Input: the current version `MAJOR.MINOR.PATCH[-PRE]` and the list of classified changes.

| Highest change class | Current is 0.y.z | Current is >=1.0.0 |
|---|---|---|
| breaking (removed/renamed public API, changed default behaviour, raised minimum runtime) | bump MINOR, reset PATCH | bump MAJOR, reset MINOR and PATCH |
| feature (new public API, new optional parameter, new CLI flag) | bump PATCH | bump MINOR, reset PATCH |
| fix (bug fix, perf without API change, docs in published package) | bump PATCH | bump PATCH |
| internal (tests, CI, refactor with identical behaviour, dev-only deps) | no release | no release |

Edge cases:

1. Pre-release current version (`1.4.0-rc.2`): if the highest class is not above what the
   pre-release already planned, only increment the pre-release number (`1.4.0-rc.3`); a
   final release drops the suffix (`1.4.0`).
2. Deprecation without removal is a feature, not breaking.
3. Fixing a bug that users may rely on (documented behaviour was wrong) is breaking.
4. Security fixes that require a config change are breaking; otherwise fix.
5. A dependency bump is classified by its effect on *our* public API, not by the dependency's own bump.
6. Version `0.0.z`: every change, including features, bumps PATCH only.
