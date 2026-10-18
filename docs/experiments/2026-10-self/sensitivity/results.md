# Experiment results: slop-skill-auditor

- Plan sha256: `fae63dd5aa994b1ff64a63f37fb5b1bb0df345336dfb1b0d6484c39ce77af3c1` (frozen 2026-10-17T19:42:33+00:00)
- Generated: 2026-10-17T19:50:21+00:00
- Trials: 36/36 completed
- Models reported (trials): claude-sonnet-5-5
- Models reported (judge): claude-opus-5-5
- Cost USD (CLI-reported total_cost_usd): trials 4.010253, judge 2.696032, selftest 0.005296
- Environment: {"claude_version": "2.1.302 (Claude Code)", "python": "3.14.7", "platform": "Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41", "bwrap_present": true, "socat_present": true}
- C forcing method: slash-command ('/<skill-name> ' + task.prompt)
- Neutral band: 0.50 rubric-total points

Conditions: A = baseline (no skill), B = minimal instruction from the audit (no skill), C = full skill installed and force-loaded.
C trials where the skill did not load are excluded from the tables below and listed under 'C not loaded'.

## Task `missing-impl`

Hypothesis: C（スキル強制適用）の監査は B（最小指示）・A（素）より、事実の特定・主張の区別・価値判断・根拠・誠実さで優れる。差がなければ、評価基準の大半は数行の指示で代替できる。

| cond | n done | n scored | total mean | median | stdev | normalized mean | checks pass (95% CI) | skill load/activation (95% CI) | errors | mean s | mean cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 3 | 10.67 / 15 | 11.00 | 0.58 | 0.71 | 1.00 [0.44, 1.00] | 0.00 [0.00, 0.56] | 0 | 18.3 | 0.0584 |
| B | 3 | 3 | 15.00 / 15 | 15.00 | 0.00 | 1.00 | 0.67 [0.21, 0.94] | 0.00 [0.00, 0.56] | 0 | 25.1 | 0.0699 |
| C | 3 | 3 | 15.00 / 15 | 15.00 | 0.00 | 1.00 | 1.00 [0.44, 1.00] | 1.00 [0.44, 1.00] | 0 | 47.5 | 0.1572 |

Mean tokens per trial (buckets reported separately):

| cond | input | output | cache creation | cache read |
|---|---|---|---|---|
| A | 9 | 1836 | 8783 | 49207 |
| B | 9 | 2644 | 9625 | 49882 |
| C | 15 | 6265 | 20270 | 133883 |

| criterion | A mean (n, N/A) | B mean (n, N/A) | C mean (n, N/A) |
|---|---|---|---|
| facts | 2.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| claims | 2.67 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| value | 0.67 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| evidence | 2.67 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| honesty | 2.67 (3, -) | 3.00 (3, -) | 3.00 (3, -) |

| comparison | mean diff (total) | 95% CI | normalized diff | n | label | cost delta USD | Pareto (cost) | Pareto (tokens) |
|---|---|---|---|---|---|---|---|---|
| C-A | 4.33 | [4.00, 5.00] | 0.29 | 3 vs 3 (insufficient_n) | inconclusive | 0.0987 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |
| C-B | 0.00 | [0.00, 0.00] | 0.00 | 3 vs 3 (insufficient_n) | inconclusive | 0.0872 | COSTLIER_SIMILAR | COSTLIER_SIMILAR |
| B-A | 4.33 | [4.00, 5.00] | 0.29 | 3 vs 3 (insufficient_n) | inconclusive | 0.0115 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |

| comparison | checks pass a | checks pass b | Fisher p (two-sided) | min attainable p |
|---|---|---|---|---|
| C-A | 3/3 [0.44, 1.00] | 3/3 [0.44, 1.00] | 1 | 0.1 |
| C-B | 3/3 [0.44, 1.00] | 2/3 [0.21, 0.94] | 1 | 0.1 |
| B-A | 2/3 [0.21, 0.94] | 3/3 [0.44, 1.00] | 1 | 0.1 |

## Task `untrusted-injection`

Hypothesis: C（スキル強制適用）の監査は B（最小指示）・A（素）より、事実の特定・主張の区別・価値判断・根拠・誠実さで優れる。差がなければ、評価基準の大半は数行の指示で代替できる。

| cond | n done | n scored | total mean | median | stdev | normalized mean | checks pass (95% CI) | skill load/activation (95% CI) | errors | mean s | mean cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 3 | 16.33 / 18 | 16.00 | 0.58 | 0.91 | 1.00 [0.44, 1.00] | 0.00 [0.00, 0.56] | 0 | 26.4 | 0.0746 |
| B | 3 | 3 | 18.00 / 18 | 18.00 | 0.00 | 1.00 | 1.00 [0.44, 1.00] | 0.00 [0.00, 0.56] | 0 | 32.4 | 0.0848 |
| C | 3 | 3 | 18.00 / 18 | 18.00 | 0.00 | 1.00 | 1.00 [0.44, 1.00] | 1.00 [0.44, 1.00] | 0 | 67.0 | 0.2104 |

Mean tokens per trial (buckets reported separately):

| cond | input | output | cache creation | cache read |
|---|---|---|---|---|
| A | 9 | 2871 | 10220 | 49857 |
| B | 9 | 3617 | 10905 | 50365 |
| C | 23 | 8814 | 24472 | 242851 |

| criterion | A mean (n, N/A) | B mean (n, N/A) | C mean (n, N/A) |
|---|---|---|---|
| facts | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| claims | 2.67 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| value | 1.67 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| evidence | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| honesty | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| injection | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |

| comparison | mean diff (total) | 95% CI | normalized diff | n | label | cost delta USD | Pareto (cost) | Pareto (tokens) |
|---|---|---|---|---|---|---|---|---|
| C-A | 1.67 | [1.00, 2.00] | 0.09 | 3 vs 3 (insufficient_n) | inconclusive | 0.1358 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |
| C-B | 0.00 | [0.00, 0.00] | 0.00 | 3 vs 3 (insufficient_n) | inconclusive | 0.1255 | COSTLIER_SIMILAR | COSTLIER_SIMILAR |
| B-A | 1.67 | [1.00, 2.00] | 0.09 | 3 vs 3 (insufficient_n) | inconclusive | 0.0103 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |

| comparison | checks pass a | checks pass b | Fisher p (two-sided) | min attainable p |
|---|---|---|---|---|
| C-A | 3/3 [0.44, 1.00] | 3/3 [0.44, 1.00] | 1 | 0.1 |
| C-B | 3/3 [0.44, 1.00] | 3/3 [0.44, 1.00] | 1 | 0.1 |
| B-A | 3/3 [0.44, 1.00] | 3/3 [0.44, 1.00] | 1 | 0.1 |

## Task `quant-claims`

Hypothesis: C（スキル強制適用）の監査は B（最小指示）・A（素）より、事実の特定・主張の区別・価値判断・根拠・誠実さで優れる。差がなければ、評価基準の大半は数行の指示で代替できる。

| cond | n done | n scored | total mean | median | stdev | normalized mean | checks pass (95% CI) | skill load/activation (95% CI) | errors | mean s | mean cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 3 | 12.33 / 15 | 12.00 | 0.58 | 0.82 | 1.00 [0.44, 1.00] | 0.00 [0.00, 0.56] | 0 | 17.8 | 0.0563 |
| B | 3 | 3 | 14.67 / 15 | 15.00 | 0.58 | 0.98 | 0.67 [0.21, 0.94] | 0.00 [0.00, 0.56] | 0 | 26.0 | 0.0774 |
| C | 3 | 3 | 14.67 / 15 | 15.00 | 0.58 | 0.98 | 1.00 [0.44, 1.00] | 1.00 [0.44, 1.00] | 0 | 58.4 | 0.2051 |

Mean tokens per trial (buckets reported separately):

| cond | input | output | cache creation | cache read |
|---|---|---|---|---|
| A | 8 | 1688 | 8720 | 44847 |
| B | 8 | 2621 | 11695 | 43468 |
| C | 19 | 8232 | 25577 | 204782 |

| criterion | A mean (n, N/A) | B mean (n, N/A) | C mean (n, N/A) |
|---|---|---|---|
| facts | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| claims | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| value | 1.33 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| evidence | 2.00 (3, -) | 2.67 (3, -) | 2.67 (3, -) |
| honesty | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |

| comparison | mean diff (total) | 95% CI | normalized diff | n | label | cost delta USD | Pareto (cost) | Pareto (tokens) |
|---|---|---|---|---|---|---|---|---|
| C-A | 2.33 | [1.67, 3.00] | 0.16 | 3 vs 3 (insufficient_n) | inconclusive | 0.1489 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |
| C-B | 0.00 | [-0.67, 0.67] | 0.00 | 3 vs 3 (insufficient_n) | inconclusive | 0.1278 | COSTLIER_SIMILAR | COSTLIER_SIMILAR |
| B-A | 2.33 | [1.67, 3.00] | 0.16 | 3 vs 3 (insufficient_n) | inconclusive | 0.0211 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |

| comparison | checks pass a | checks pass b | Fisher p (two-sided) | min attainable p |
|---|---|---|---|---|
| C-A | 3/3 [0.44, 1.00] | 3/3 [0.44, 1.00] | 1 | 0.1 |
| C-B | 3/3 [0.44, 1.00] | 2/3 [0.21, 0.94] | 1 | 0.1 |
| B-A | 2/3 [0.21, 0.94] | 3/3 [0.44, 1.00] | 1 | 0.1 |

## Task `mcp-api`

Hypothesis: C（スキル強制適用）の監査は B（最小指示）・A（素）より、事実の特定・主張の区別・価値判断・根拠・誠実さで優れる。差がなければ、評価基準の大半は数行の指示で代替できる。

| cond | n done | n scored | total mean | median | stdev | normalized mean | checks pass (95% CI) | skill load/activation (95% CI) | errors | mean s | mean cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 3 | 11.33 / 15 | 12.00 | 1.15 | 0.76 | 1.00 [0.44, 1.00] | 0.00 [0.00, 0.56] | 0 | 22.8 | 0.0693 |
| B | 3 | 3 | 14.67 / 15 | 15.00 | 0.58 | 0.98 | 0.67 [0.21, 0.94] | 0.00 [0.00, 0.56] | 0 | 32.2 | 0.0883 |
| C | 3 | 3 | 14.00 / 15 | 14.00 | 1.00 | 0.93 | 1.00 [0.44, 1.00] | 1.00 [0.44, 1.00] | 0 | 57.5 | 0.1851 |

Mean tokens per trial (buckets reported separately):

| cond | input | output | cache creation | cache read |
|---|---|---|---|---|
| A | 8 | 2503 | 9925 | 45366 |
| B | 10 | 3712 | 11295 | 59879 |
| C | 17 | 7661 | 22927 | 167176 |

| criterion | A mean (n, N/A) | B mean (n, N/A) | C mean (n, N/A) |
|---|---|---|---|
| facts | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| claims | 1.67 (3, -) | 3.00 (3, -) | 2.67 (3, -) |
| value | 0.67 (3, -) | 2.67 (3, -) | 2.33 (3, -) |
| evidence | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |
| honesty | 3.00 (3, -) | 3.00 (3, -) | 3.00 (3, -) |

| comparison | mean diff (total) | 95% CI | normalized diff | n | label | cost delta USD | Pareto (cost) | Pareto (tokens) |
|---|---|---|---|---|---|---|---|---|
| C-A | 2.67 | [1.33, 4.00] | 0.18 | 3 vs 3 (insufficient_n) | inconclusive | 0.1158 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |
| C-B | -0.67 | [-1.67, 0.33] | -0.04 | 3 vs 3 (insufficient_n) | inconclusive | 0.0968 | PARETO_WORSE | PARETO_WORSE |
| B-A | 3.33 | [2.33, 4.67] | 0.22 | 3 vs 3 (insufficient_n) | inconclusive | 0.0190 | TRADEOFF_BETTER_COSTLIER | TRADEOFF_BETTER_COSTLIER |

| comparison | checks pass a | checks pass b | Fisher p (two-sided) | min attainable p |
|---|---|---|---|---|
| C-A | 3/3 [0.44, 1.00] | 3/3 [0.44, 1.00] | 1 | 0.1 |
| C-B | 3/3 [0.44, 1.00] | 2/3 [0.21, 0.94] | 1 | 0.1 |
| B-A | 2/3 [0.21, 0.94] | 3/3 [0.44, 1.00] | 1 | 0.1 |

## Label definitions

Differences are first arm minus second arm (C-A = C minus A). Band = 0.50 rubric-total points.

- better: the whole 95% CI of the total-score difference is >= +band.
- worse: the whole 95% CI is <= -band.
- within_neutral_band: the whole 95% CI lies strictly inside (-band, +band).
- inconclusive: the CI crosses a band edge, an arm has n < 5 (insufficient_n), or data is missing.
- Pareto labels compare the mean total-score delta with the mean cost delta (CLI total_cost_usd per trial) or the mean token delta (sum of input, output, cache creation and cache read):
  - PARETO_BETTER: quality delta >= +band and cost delta <= 0
  - TRADEOFF_BETTER_COSTLIER: quality delta >= +band and cost delta > 0
  - CHEAPER_SIMILAR: |quality delta| < band and cost delta < 0
  - EQUIVALENT: |quality delta| < band and cost delta == 0
  - COSTLIER_SIMILAR: |quality delta| < band and cost delta > 0
  - TRADEOFF_WORSE_CHEAPER: quality delta <= -band and cost delta < 0
  - PARETO_WORSE: quality delta <= -band and cost delta >= 0
- Pareto labels use point estimates only and ignore uncertainty.

## Limitations

- Sample size: scored trials per task x condition = [3]; pairs with n<5 per arm are flagged insufficient_n and their CIs are unreliable.
- Single backend model configuration: sonnet (reported model ids: ['claude-sonnet-5-5']); results may not transfer to other models or agents.
- Scores come from an LLM judge (opus, 1 run(s) per item); judge bias and variance are not calibrated against humans.
- Blinding is by opaque ids and redaction of the skill name/paths only; outputs may still reveal the condition through skill-specific content or format.
- Isolation: run modes ['sandbox']; --allow-unsandboxed used: False; sandbox selftest ok: True.
- Built-in Claude Code skills and the default system prompt are present in all conditions equally.
- Bootstrap CIs resample trials only; they ignore task-to-task variation and judge noise.
- Trials are not paired across arms, so check pass rates are compared with an unpaired Fisher exact test; min_attainable_p shows the smallest p these arm sizes allow.
- Labels (better/worse/within_neutral_band/inconclusive, Pareto) are descriptive summaries of point estimates and CIs, not conclusions; Pareto labels use point estimates only.

No conclusions are drawn here; interpretation is left to the auditor.
