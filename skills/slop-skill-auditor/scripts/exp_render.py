from __future__ import annotations

import json

from exp_plan import CONDITIONS
from exp_stats import UNSTABLE_MARGIN, UNSTABLE_MIN_N


def fmt(v, digits=2):
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def fmt_val(d, digits=2):
    if d.get("value") is None:
        return f"null ({d.get('reason')})"
    return fmt(d["value"], digits)


def fmt_ci(ci, digits=2):
    return f"[{fmt(ci[0], digits)}, {fmt(ci[1], digits)}]" if ci else "null"


def fmt_p(p):
    return "null" if p is None else f"{p:.3g}"



def render_activation_md(res: dict) -> str:
    a = res["activation"]
    L = [f"# Activation experiment: {res['skill']['name']}", "",
         f"- Plan sha256: `{res['plan_sha256']}` (frozen {res['frozen_at']})",
         f"- Trials: {res['trials']['completed']}/{res['trials']['planned']} completed",
         f"- Models reported: {', '.join(res['models_reported']['trials']) or 'none'}",
         f"- Cost USD: trials {res['cost_usd']['trials']}, selftest {res['cost_usd']['selftest']}", "",
         "| task | should_activate | n valid | activated | rate | 95% CI (Wilson) | unstable | skill discovered | errors |", "|---|---|---|---|---|---|---|---|---|"]
    for t in a["per_task"]:
        ar = t["activation_rate"]
        L.append(f"| {t['task_id']} | {t['should_activate']} | {t['n_valid']} | {ar['k']} | {fmt_val(ar)} | {fmt_ci(ar.get('ci95'))} | {t['unstable']} | {fmt_val(t['skill_discovered_rate'])} | {t['error_count']} |")
    c = a["counts"]
    L += ["", f"- True positive rate: {fmt_val(a['true_positive_rate'])}, 95% CI {fmt_ci(a['true_positive_rate'].get('ci95'))} (TP={c['tp']}, FN={c['fn']})",
          f"- False positive rate: {fmt_val(a['false_positive_rate'])}, 95% CI {fmt_ci(a['false_positive_rate'].get('ci95'))} (FP={c['fp']}, TN={c['tn']})",
          f"- unstable = activation rate within {UNSTABLE_MARGIN} of 0.5 with n >= {UNSTABLE_MIN_N}.", "",
          "## Limitations", ""] + [f"- {x}" for x in res["limitations"]] + ["",
          "No conclusions are drawn here; interpretation is left to the auditor.", ""]
    return "\n".join(L)


def render_md(res: dict, tasks: dict) -> str:
    band = res["analysis"]["neutral_band"]
    L = [f"# Experiment results: {res['skill']['name']}", "",
         f"- Plan sha256: `{res['plan_sha256']}` (frozen {res['frozen_at']})",
         f"- Generated: {res['generated_at']}",
         f"- Trials: {res['trials']['completed']}/{res['trials']['planned']} completed",
         f"- Models reported (trials): {', '.join(res['models_reported']['trials']) or 'none'}",
         f"- Models reported (judge): {', '.join(res['models_reported']['judge']) or 'none'}",
         f"- Cost USD (CLI-reported total_cost_usd): trials {res['cost_usd']['trials']}, judge {res['cost_usd']['judge']}, selftest {res['cost_usd']['selftest']}",
         f"- Environment: {json.dumps(res.get('env'))}",
         f"- C forcing method: {(res.get('forcing_method') or {}).get('name')} ({(res.get('forcing_method') or {}).get('prompt')})",
         f"- Neutral band: {fmt(band)} rubric-total points", "",
         "Conditions: A = baseline (no skill), B = minimal instruction from the audit (no skill), C = full skill installed and force-loaded.",
         "C trials where the skill did not load are excluded from the tables below and listed under 'C not loaded'.", ""]
    if res.get("reference_flags"):
        L += ["## WARNING: degenerate outputs score as well as real conditions", ""]
        L += [f"- **Task `{f['task_id']}`: the rubric does not separate {', '.join(f['beats_or_ties'])} from the degenerate output "
              f"`{f['reference_id']}`** (reference total {fmt(f['reference_total'])} >= those conditions' mean totals)." for f in res["reference_flags"]]
        L.append("")
    for task_id, task in tasks.items():
        L += [f"## Task `{task_id}`", "", f"Hypothesis: {task.get('hypothesis') or '(none)'}", "",
              "| cond | n done | n scored | total mean | median | stdev | normalized mean | checks pass (95% CI) | skill load/activation (95% CI) | errors | mean s | mean cost USD |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for cond in CONDITIONS:
            c = res["cells"][f"{task_id}/{cond}"]
            ts = c["total_score"]
            sr, sa = c["success_rate"], c["skill_activation_rate"]
            L.append(f"| {cond} | {c['n_completed']} | {c['n_scored']} | {fmt(ts['mean'])} / {c['max_total_score']} | {fmt(ts['median'])} | {fmt(ts['stdev'])} "
                     f"| {fmt(c['normalized_score']['mean'])} | {fmt_val(sr)} {fmt_ci(sr.get('ci95'))} | {fmt_val(sa)} {fmt_ci(sa.get('ci95'))} | {c['error_count']} "
                     f"| {fmt_val(c['duration_s'], 1)} | {fmt_val(c['cost_usd'], 4)} |")
        L += ["", "Mean tokens per trial (buckets reported separately):", "",
              "| cond | input | output | cache creation | cache read |", "|---|---|---|---|---|"]
        for cond in CONDITIONS:
            t = res["cells"][f"{task_id}/{cond}"]["tokens"]
            L.append(f"| {cond} | {fmt_val(t['input_tokens'], 0)} | {fmt_val(t['output_tokens'], 0)} | {fmt_val(t['cache_creation_input_tokens'], 0)} | {fmt_val(t['cache_read_input_tokens'], 0)} |")
        L += ["", "| criterion | " + " | ".join(f"{c} mean (n, N/A)" for c in CONDITIONS) + " |", "|---|" + "---|" * len(CONDITIONS)]
        for crit in task["rubric"]:
            row = []
            for c in CONDITIONS:
                cell = res["cells"][f"{task_id}/{c}"]
                d = cell["criteria"][crit["id"]]
                row.append(f"{fmt(d['mean'])} ({d['n']}, {cell['criteria_na_counts'].get(crit['id'], '-')})")
            L.append(f"| {crit['id']}{' (optional)' if crit.get('optional') else ''} | " + " | ".join(row) + " |")
        refs = [r for r in res.get("references", []) if r["task_id"] == task_id]
        if refs:
            L += ["", "Reference (degenerate) outputs, no model call; not conditions and never in the comparisons below:", "",
                  "| reference | kind | description | total | normalized | checks passed | saturated criteria | note |", "|---|---|---|---|---|---|---|---|"]
            for r in refs:
                sat = next((x["criteria"] for x in res.get("saturated_criteria", []) if x["task_id"] == task_id and x["reference_id"] == r["reference_id"]), [])
                L.append(f"| REF:{r['reference_id']} | {r['kind']} | {r['description']} | {fmt(r['total_score'])} / {res['cells'][f'{task_id}/A']['max_total_score']} "
                         f"| {fmt(r['normalized_score'])} | {fmt(r['checks_passed'])} | {', '.join(sat) or '-'} | {r['reason'] or ''} |")
        metric_ids = list(res["cells"][f"{task_id}/A"].get("metrics", {}))
        if metric_ids:
            L += ["", "Metrics (never folded into rubric totals):", "",
                  "| metric | direction | " + " | ".join(f"{c} mean / median / stdev (n, null)" for c in CONDITIONS) + " | " + " | ".join(f"REF:{r['reference_id']}" for r in refs) + " |",
                  "|---|---|" + "---|" * (len(CONDITIONS) + len(refs))]
            for mid in metric_ids:
                cols = []
                for c in CONDITIONS:
                    d = res["cells"][f"{task_id}/{c}"]["metrics"][mid]
                    cols.append(f"{fmt(d['mean'])} / {fmt(d['median'])} / {fmt(d['stdev'])} ({d['n']}, {d['n_null']})")
                cols += [fmt(r["metrics"].get(mid)) for r in refs]
                L.append(f"| {mid} | {res['cells'][f'{task_id}/A']['metrics'][mid]['direction']} | " + " | ".join(cols) + " |")
            L += ["", "| comparison | metric | mean diff | 95% CI (bootstrap) | n | reading |", "|---|---|---|---|---|---|"]
            for pw in [x for x in res["pairwise"] if x["task_id"] == task_id]:
                for mid, me in pw.get("metrics", {}).items():
                    ci = fmt_ci(me["ci95"]) if me.get("ci95") else f"null ({me.get('reason')})"
                    L.append(f"| {pw['comparison']} | {mid} | {fmt(me['mean_diff'])} | {ci} | {me['n_a']} vs {me['n_b']} | {me['interpretation']} |")
        L += ["", "| comparison | mean diff (total) | 95% CI | normalized diff | n | label | cost delta USD | Pareto (cost) | Pareto (tokens) |",
              "|---|---|---|---|---|---|---|---|---|"]
        pairs = [p for p in res["pairwise"] if p["task_id"] == task_id]
        for p in pairs:
            ci = fmt_ci(p["ci95"]) if p.get("ci95") else f"null ({p.get('reason')})"
            L.append(f"| {p['comparison']} | {fmt(p['mean_diff'])} | {ci} | {fmt(p['normalized_mean_diff'])} | {p['n_a']} vs {p['n_b']}"
                     f"{' (insufficient_n)' if p['insufficient_n'] else ''} | {p['label']} | {fmt(p['cost_delta_usd'], 4)} | {p['pareto_cost']} | {p['pareto_tokens']} |")
        if any("checks" in p for p in pairs):
            L += ["", "| comparison | checks pass a | checks pass b | Fisher p (two-sided) | min attainable p |", "|---|---|---|---|---|"]
            for p in pairs:
                ch = p.get("checks")
                if not ch:
                    continue
                if "reason" in ch:
                    L.append(f"| {p['comparison']} | null | null | null ({ch['reason']}) | null |")
                else:
                    L.append(f"| {p['comparison']} | {ch['pass_a']}/{ch['n_a']} {fmt_ci(ch['ci95_a'])} | {ch['pass_b']}/{ch['n_b']} {fmt_ci(ch['ci95_b'])} "
                             f"| {fmt_p(ch['fisher_p_two_sided'])} | {fmt_p(ch['min_attainable_p'])} |")
        L.append("")
    L += ["## Label definitions", "",
          f"Differences are first arm minus second arm (C-A = C minus A). Band = {fmt(band)} rubric-total points.", "",
          "- better: the whole 95% CI of the total-score difference is >= +band.",
          "- worse: the whole 95% CI is <= -band.",
          "- within_neutral_band: the whole 95% CI lies strictly inside (-band, +band).",
          "- inconclusive: the CI crosses a band edge, an arm has n < 5 (insufficient_n), or data is missing.",
          "- Pareto labels compare the mean total-score delta with the mean cost delta (CLI total_cost_usd per trial) or the mean token delta (sum of input, output, cache creation and cache read):"]
    L += [f"  - {k}: {v}" for k, v in res["analysis"]["pareto_definitions"].items()]
    L += ["- Pareto labels use point estimates only and ignore uncertainty.", ""]
    if res.get("c_not_loaded"):
        L += ["## C not loaded (excluded)", ""] + [f"- trial `{m['trial_id']}` (task {m['task_id']}): total {fmt(m['total_score'])}; {m['reason']}" for m in res["c_not_loaded"]] + [""]
    if res["missing"]:
        L += ["## Missing values", ""] + [f"- trial `{m['trial_id']}`: {m['reason']}" for m in res["missing"]] + [""]
    L += ["## Limitations", ""] + [f"- {x}" for x in res["limitations"]] + ["",
          "No conclusions are drawn here; interpretation is left to the auditor.", ""]
    return "\n".join(L)
