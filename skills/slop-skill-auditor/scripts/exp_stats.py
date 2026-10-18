from __future__ import annotations

import hashlib
import math
import random
import statistics

BOOTSTRAP_RESAMPLES = 10000
MIN_N = 5
Z95 = 1.959963984540054
UNSTABLE_MARGIN = 0.2
UNSTABLE_MIN_N = 3
PARETO_DEFINITIONS = {
    "PARETO_BETTER": "quality delta >= +band and cost delta <= 0",
    "TRADEOFF_BETTER_COSTLIER": "quality delta >= +band and cost delta > 0",
    "CHEAPER_SIMILAR": "|quality delta| < band and cost delta < 0",
    "EQUIVALENT": "|quality delta| < band and cost delta == 0",
    "COSTLIER_SIMILAR": "|quality delta| < band and cost delta > 0",
    "TRADEOFF_WORSE_CHEAPER": "quality delta <= -band and cost delta < 0",
    "PARETO_WORSE": "quality delta <= -band and cost delta >= 0",
}


def describe(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "stdev": None, "reason": "no values"}
    d = {"n": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
         "stdev": statistics.stdev(values) if len(values) > 1 else None}
    if len(values) < 2:
        d["reason"] = "stdev needs n>=2"
    return d


def mean_or_null(values, what):
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return {"value": None, "reason": f"no {what} reported"}
    out = {"value": statistics.fmean(vals), "n": len(vals)}
    if len(vals) < len(values):
        out["missing"] = len(values) - len(vals)
    return out


def derived_seed(seed: int, *parts) -> int:
    return int(hashlib.sha256("|".join(map(str, (seed, *parts))).encode()).hexdigest()[:16], 16)


def bootstrap_diff_ci(x: list[float], y: list[float], seed: int, resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float]:
    rng = random.Random(seed)
    nx, ny = len(x), len(y)
    diffs = sorted(sum(rng.choices(x, k=nx)) / nx - sum(rng.choices(y, k=ny)) / ny for _ in range(resamples))
    lo = diffs[int(0.025 * (resamples - 1))]
    hi = diffs[int(round(0.975 * (resamples - 1)))]
    return lo, hi


def wilson(k: int, n: int) -> list[float] | None:
    if not n:
        return None
    phat = k / n
    denom = 1 + Z95 ** 2 / n
    centre = (phat + Z95 ** 2 / (2 * n)) / denom
    half = Z95 * math.sqrt(phat * (1 - phat) / n + Z95 ** 2 / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def rate(k, n, what):
    if not n:
        return {"value": None, "k": 0, "n": 0, "ci95": None, "reason": f"no {what}"}
    return {"value": k / n, "k": k, "n": n, "ci95": wilson(k, n), "ci_method": "Wilson score interval"}


def fisher_exact(k1: int, n1: int, k2: int, n2: int) -> float:
    total, col = n1 + n2, k1 + k2
    denom = math.comb(total, col)
    probs = {x: math.comb(n1, x) * math.comb(n2, col - x) / denom for x in range(max(0, col - n2), min(n1, col) + 1)}
    observed = probs[k1]
    return min(1.0, sum(p for p in probs.values() if p <= observed * (1 + 1e-7)))


def min_attainable_p(n1: int, n2: int) -> float:
    return min(fisher_exact(k1, n1, k2, n2) for k1 in range(n1 + 1) for k2 in range(n2 + 1))


def band_label(diff, ci, band, insufficient) -> str:
    if diff is None or ci is None or insufficient:
        return "inconclusive"
    lo, hi = ci
    if lo >= band:
        return "better"
    if hi <= -band:
        return "worse"
    if lo > -band and hi < band:
        return "within_neutral_band"
    return "inconclusive"


def pareto_label(dq, dc, band) -> str | None:
    if dq is None or dc is None:
        return None
    if dq >= band:
        return "PARETO_BETTER" if dc <= 0 else "TRADEOFF_BETTER_COSTLIER"
    if dq <= -band:
        return "PARETO_WORSE" if dc >= 0 else "TRADEOFF_WORSE_CHEAPER"
    return "CHEAPER_SIMILAR" if dc < 0 else ("COSTLIER_SIMILAR" if dc > 0 else "EQUIVALENT")


def diff_or_none(a, b):
    return a - b if a is not None and b is not None else None


def item_scores(grade: dict | None, rubric: list[dict]) -> dict:
    out = {"per": {}, "total": None, "normalized": None, "na": [], "reason": None}
    if grade is None:
        out["reason"] = "no grade file"
        return out
    runs = scores_per_run(grade)
    missing = []
    for c in rubric:
        given = [r[c["id"]] for r in runs if c["id"] in r]
        vals = [v for v in given if isinstance(v, int) and not isinstance(v, bool)]
        if vals:
            out["per"][c["id"]] = statistics.fmean(vals)
            continue
        out["per"][c["id"]] = None
        (out["na"] if c.get("optional") and given else missing).append(c["id"])
    if missing:
        out["reason"] = f"missing scores for {missing}"
        return out
    scored = [c for c in rubric if out["per"][c["id"]] is not None]
    if not scored:
        out["reason"] = "every criterion was N/A"
        return out
    out["total"] = sum(out["per"][c["id"]] for c in scored)
    span = sum(max(c["scale"]) - min(c["scale"]) for c in scored)
    out["normalized"] = sum(out["per"][c["id"]] - min(c["scale"]) for c in scored) / span if span else None
    return out


def scores_per_run(grade: dict | None) -> list[dict]:
    if not grade:
        return []
    return [r["scores"] for r in grade.get("runs", []) if isinstance(r.get("scores"), dict)]


def metric_values(rs: list[dict], metric_id: str) -> tuple[list[float], list[str]]:
    vals, reasons = [], []
    for r in rs:
        rec = next((m for m in r.get("metrics") or [] if m.get("id") == metric_id), None)
        if rec is None:
            reasons.append(f"{r['trial_id']}: metric not recorded")
        elif rec.get("value") is None:
            reasons.append(f"{r['trial_id']}: {rec.get('reason')}")
        else:
            vals.append(rec["value"])
    return vals, reasons


def metric_direction_note(direction: str, a: str, b: str) -> str:
    if direction == "higher_better":
        return f"positive favours {a} over {b}"
    if direction == "lower_better":
        return f"negative favours {a} over {b}"
    return "informational: no better direction"
