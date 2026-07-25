"""Coarse search over the risk-blend constants against an explicit objective.

Run: python scripts/tune_blend.py [--records PATH] [--top N]

**Objective** (agreed before running, not chosen to flatter the result):

    minimize   MEDIUM's share of the estimated account population
    subject to precision strictly increasing LOW < MEDIUM < HIGH < CRITICAL
               recall at HIGH+    >= 7.2%   (the current value, no slack)
               recall at MEDIUM+  >= 94.8%  (the current value, no slack)

The MEDIUM+ recall floor is the important one. Without it the cheapest way to shrink MEDIUM is
to push true positives *down* into LOW, which improves the objective while making the product
worse. Monotonicity alone does not catch that, because precision can stay ordered while recall
drains away.

Why minimize MEDIUM's share rather than maximize CRITICAL precision: the validated table has
CRITICAL at 25/25 sampled with no observed false positives, so its precision is already at the
measurement ceiling and unmeasurable from above. Optimizing it would just make CRITICAL
stricter and drive its 4.2% recall toward zero. MEDIUM holding ~62% of the population at 1.8%
precision is the actual defect.

Replays `risk()` over the cached `anomaly()` output from scripts/validate_risk_levels.py, so no
account is re-scored. Scores depend only on the three weights, never on the thresholds, so the
search computes 2,000 scores once per weight triple and then evaluates every threshold
combination against that vector - the alternative is millions of redundant risk() calls.
"""
import argparse
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ml.risk import DEFAULT_BLEND, RISK_ORDER_FLOORS, risk  # noqa: E402
from ml.validation import RISK_ORDER  # noqa: E402

from ml.data import MODEL_DIR as _MODEL_DIR  # noqa: E402
RECORDS_PATH = Path(_MODEL_DIR) / "risk_validation_records.json"
OUT_PATH = Path(_MODEL_DIR) / "blend_tuning.json"

# Recall floors are read off the baseline at runtime, not hardcoded: the table in
# ml_spec.md rounds them to 7.2% / 94.8%, and hardcoding the rounded values made the current
# constants fail their own constraint (true HIGH+ recall is 7.167%). "No slack" has to mean
# the measured value, not its display form.
CONSTRAINT_TOLERANCE = 1e-9

DETECTOR_WEIGHTS = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70]
RULE_BASE_CREDITS = [0.40, 0.50, 0.60, 0.70, 0.80]
BENIGN_DAMPINGS = [0.00, 0.25, 0.50, 0.75]

MEDIUM_FLOORS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
HIGH_FLOORS = [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]
CRITICAL_FLOORS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]


def load_records(path: Path) -> tuple[list[dict], dict]:
    payload = json.loads(path.read_text())
    return payload["records"], payload["weights"]


def score_sample(records: list[dict], weights: dict, blend: dict) -> list[tuple]:
    """(risk_score, is_laundering, stratum_weight) per scorable account, under one blend.

    Errored records are dropped rather than treated as clean, matching crosstab.
    """
    scored = []
    for record in records:
        if record.get("error"):
            continue
        result = risk(record["anomaly_result"], blend=blend)
        if "error" in result or result.get("risk_score") is None:
            continue
        scored.append((
            result["risk_score"],
            bool(record["is_laundering"]),
            float(weights.get(record["stratum"], 1.0)),
        ))
    return scored


def evaluate(scored: list[tuple], floors: dict[str, float]) -> dict:
    """Weighted precision / share / recall per level for one set of thresholds."""
    totals = {level: {"positive": 0.0, "negative": 0.0} for level in RISK_ORDER}
    for score, positive, weight in scored:
        if score >= floors["CRITICAL"]:
            level = "CRITICAL"
        elif score >= floors["HIGH"]:
            level = "HIGH"
        elif score >= floors["MEDIUM"]:
            level = "MEDIUM"
        else:
            level = "LOW"
        totals[level]["positive" if positive else "negative"] += weight

    population = sum(t["positive"] + t["negative"] for t in totals.values())
    positives = sum(t["positive"] for t in totals.values())

    levels = {}
    for level in RISK_ORDER:
        n = totals[level]["positive"] + totals[level]["negative"]
        levels[level] = {
            "share": (n / population) if population else 0.0,
            "precision": (totals[level]["positive"] / n) if n else None,
            "recall": (totals[level]["positive"] / positives) if positives else 0.0,
            "estimated_n": n,
        }
    return levels


def recall_at_or_above(levels: dict, level: str) -> float:
    cutoff = RISK_ORDER.index(level)
    return sum(levels[name]["recall"] for name in RISK_ORDER[cutoff:])


def constraints_met(levels: dict, recall_floors: dict[str, float]) -> tuple[bool, str]:
    """Every constraint, with the first failure named so a rejected candidate is explainable."""
    precisions = [levels[level]["precision"] for level in RISK_ORDER]
    if any(p is None for p in precisions):
        return False, "a level is empty"
    for lower, upper in zip(RISK_ORDER, RISK_ORDER[1:]):
        if not levels[lower]["precision"] < levels[upper]["precision"]:
            return False, f"precision not increasing at {lower}->{upper}"

    recall_high_plus = recall_at_or_above(levels, "HIGH")
    if recall_high_plus < recall_floors["HIGH+"] - CONSTRAINT_TOLERANCE:
        return False, f"HIGH+ recall {recall_high_plus:.3%} below floor"

    recall_medium_plus = recall_at_or_above(levels, "MEDIUM")
    if recall_medium_plus < recall_floors["MEDIUM+"] - CONSTRAINT_TOLERANCE:
        return False, f"MEDIUM+ recall {recall_medium_plus:.3%} below floor"

    return True, ""


def search(records: list[dict], weights: dict, recall_floors: dict[str, float],
           verbose: bool = True) -> dict:
    threshold_grid = [
        {"MEDIUM": m, "HIGH": h, "CRITICAL": c}
        for m, h, c in itertools.product(MEDIUM_FLOORS, HIGH_FLOORS, CRITICAL_FLOORS)
        if m < h < c
    ]
    weight_grid = list(itertools.product(DETECTOR_WEIGHTS, RULE_BASE_CREDITS, BENIGN_DAMPINGS))
    if verbose:
        print(f"grid: {len(weight_grid)} weight triples x {len(threshold_grid)} threshold sets "
              f"= {len(weight_grid) * len(threshold_grid):,} candidates")

    feasible: list[dict] = []
    rejections: dict[str, int] = {}
    evaluated = 0

    for i, (detector_weight, base_credit, damping) in enumerate(weight_grid):
        blend = {
            "detector_weight": detector_weight,
            "rule_base_credit": base_credit,
            "benign_damping": damping,
            "thresholds": DEFAULT_BLEND["thresholds"],
        }
        scored = score_sample(records, weights, blend)

        for threshold_floors in threshold_grid:
            levels = evaluate(scored, threshold_floors)
            evaluated += 1
            ok, why = constraints_met(levels, recall_floors)
            if not ok:
                rejections[why.split(" ")[0]] = rejections.get(why.split(" ")[0], 0) + 1
                continue
            feasible.append({
                "detector_weight": detector_weight,
                "rule_base_credit": base_credit,
                "benign_damping": damping,
                "thresholds": dict(threshold_floors),
                "medium_share": levels["MEDIUM"]["share"],
                "levels": levels,
            })

        if verbose and (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(weight_grid)} weight triples, "
                  f"{len(feasible):,} feasible so far")

    feasible.sort(key=lambda c: c["medium_share"])
    return {"feasible": feasible, "evaluated": evaluated, "rejections": rejections}


def as_thresholds(floors: dict[str, float]) -> list:
    """Floors dict -> the descending [(level, floor)] form RISK_THRESHOLDS uses."""
    return [
        ("CRITICAL", floors["CRITICAL"]),
        ("HIGH", floors["HIGH"]),
        ("MEDIUM", floors["MEDIUM"]),
        ("LOW", 0.0),
    ]


def print_distribution(title: str, levels: dict) -> None:
    print(f"\n{title}")
    print("| Level | Est. population n | Share | Precision | Recall |")
    print("|---|---|---|---|---|")
    for level in RISK_ORDER:
        row = levels[level]
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.2%}"
        print(f"| {level} | {row['estimated_n']:,.0f} | {row['share']:.1%} "
              f"| {precision} | {row['recall']:.1%} |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=Path, default=RECORDS_PATH)
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    if not args.records.exists():
        raise SystemExit(
            f"{args.records} not found - run scripts/validate_risk_levels.py first"
        )

    records, weights = load_records(args.records)
    print(f"loaded {len(records):,} scored accounts")

    baseline_scored = score_sample(records, weights, DEFAULT_BLEND)
    baseline_levels = evaluate(baseline_scored, RISK_ORDER_FLOORS)
    print_distribution("BEFORE (current constants)", baseline_levels)
    recall_floors = {
        "HIGH+": recall_at_or_above(baseline_levels, "HIGH"),
        "MEDIUM+": recall_at_or_above(baseline_levels, "MEDIUM"),
    }
    print(f"\nrecall floors taken from the baseline: "
          f"HIGH+ >= {recall_floors['HIGH+']:.3%}, MEDIUM+ >= {recall_floors['MEDIUM+']:.3%}")
    ok, why = constraints_met(baseline_levels, recall_floors)
    print(f"baseline satisfies its own constraints: {ok}" + ("" if ok else f" ({why})"))

    result = search(records, weights, recall_floors)
    print(f"\nevaluated {result['evaluated']:,} candidates, "
          f"{len(result['feasible']):,} feasible")
    print(f"rejection reasons: {result['rejections']}")

    if not result["feasible"]:
        print("\nNo feasible candidate. The constraints as stated admit nothing - "
              "report this rather than quietly relaxing a floor.")
        return

    best = result["feasible"][0]
    print(f"\nbest: detector_weight={best['detector_weight']}, "
          f"rule_base_credit={best['rule_base_credit']}, "
          f"benign_damping={best['benign_damping']}, thresholds={best['thresholds']}")
    print_distribution("AFTER (tuned constants)", best["levels"])

    print(f"\ntop {args.top} feasible by MEDIUM share:")
    for candidate in result["feasible"][:args.top]:
        print(f"  MEDIUM {candidate['medium_share']:.1%} | "
              f"dw={candidate['detector_weight']} bc={candidate['rule_base_credit']} "
              f"bd={candidate['benign_damping']} {candidate['thresholds']}")

    OUT_PATH.write_text(json.dumps({
        "objective": {
            "minimize": "MEDIUM share of estimated population",
            "constraints": {
                "precision_monotonic": "LOW < MEDIUM < HIGH < CRITICAL",
                "min_recall_high_plus": recall_floors["HIGH+"],
                "min_recall_medium_plus": recall_floors["MEDIUM+"],
            },
        },
        "baseline": baseline_levels,
        "best": best,
        "feasible_count": len(result["feasible"]),
        "evaluated": result["evaluated"],
        "top": result["feasible"][:args.top],
    }, indent=2))
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()


# --- trade-off frontier -----------------------------------------------------------------
#
# The first objective (minimize MEDIUM's share alone) was gameable: the search squeezed MEDIUM
# to a 0.05-wide band and moved 80% of the population into HIGH, satisfying every constraint
# while leaving a dumping ground exactly where it was. The fix is to score the *largest*
# actionable tier rather than one named tier, so relabelling the bucket gains nothing.
#
# The open question that answers is whether a spread distribution exists at all under a 94.8%
# MEDIUM+ recall floor, or whether that floor forces some tier to swallow the population. The
# frontier below varies the floor and reports the best achievable spread at each.

ACTIONABLE = ("MEDIUM", "HIGH", "CRITICAL")


def max_actionable_share(levels: dict) -> float:
    return max(levels[level]["share"] for level in ACTIONABLE)


def frontier(records: list[dict], weights: dict, medium_floors: list[float],
             high_plus_floor: float) -> list[dict]:
    """Best achievable 'largest actionable tier' at each MEDIUM+ recall floor."""
    threshold_grid = [
        {"MEDIUM": m, "HIGH": h, "CRITICAL": c}
        for m, h, c in itertools.product(MEDIUM_FLOORS, HIGH_FLOORS, CRITICAL_FLOORS)
        if m < h < c
    ]
    weight_grid = list(itertools.product(DETECTOR_WEIGHTS, RULE_BASE_CREDITS, BENIGN_DAMPINGS))

    scored_cache = []
    for detector_weight, base_credit, damping in weight_grid:
        blend = {
            "detector_weight": detector_weight,
            "rule_base_credit": base_credit,
            "benign_damping": damping,
            "thresholds": DEFAULT_BLEND["thresholds"],
        }
        scored_cache.append(
            ((detector_weight, base_credit, damping), score_sample(records, weights, blend))
        )

    results = []
    for medium_floor in medium_floors:
        recall_floors = {"HIGH+": high_plus_floor, "MEDIUM+": medium_floor}
        best = None
        for (detector_weight, base_credit, damping), scored in scored_cache:
            for threshold_floors in threshold_grid:
                levels = evaluate(scored, threshold_floors)
                ok, _ = constraints_met(levels, recall_floors)
                if not ok:
                    continue
                spread = max_actionable_share(levels)
                if best is None or spread < best["max_actionable_share"]:
                    best = {
                        "medium_plus_recall_floor": medium_floor,
                        "detector_weight": detector_weight,
                        "rule_base_credit": base_credit,
                        "benign_damping": damping,
                        "thresholds": dict(threshold_floors),
                        "max_actionable_share": spread,
                        "levels": levels,
                    }
        results.append(best or {"medium_plus_recall_floor": medium_floor, "infeasible": True})
    return results
