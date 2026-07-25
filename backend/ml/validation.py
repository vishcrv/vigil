"""Cross-tabulation of pipeline risk levels against ground truth.

Answers the question the flagged-items table actually raises — *of the accounts we label
CRITICAL, what fraction are really laundering?* — which per-rule precision and per-detector
average precision do not.

**Why stratified, and why the weighting matters.** Laundering-involved accounts are 1.23% of
the population (6,357 of 515,080). A plain random sample of the few thousand accounts that fit
in a reasonable runtime would contain ~25 positives, which cannot support a per-level precision
figure. So the sample is drawn separately from the laundering and clean strata and each
observation is re-weighted back to its population share (Horvitz-Thompson). Skipping that step
and reporting raw sampled proportions would overstate precision by more than an order of
magnitude, since the clean stratum is deliberately under-sampled.

The estimator is only as good as its rarest cell: one sampled clean account landing in CRITICAL
carries a weight of several hundred estimated false positives. `crosstab` therefore reports raw
counts alongside every estimate and marks any level whose cells are too thin to trust.
"""
from typing import Any, Iterable, Sequence

RISK_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

# Below this many sampled accounts in a cell, the precision estimate for that level is driven
# by single observations and is reported as noisy rather than quietly presented as a rate.
MIN_CELL_FOR_STABLE_ESTIMATE = 10

_WILSON_Z = 1.96


def wilson_interval(hits: int, precision: float, z: float = _WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for an observed proportion. Returns (low, high), clipped to [0,1].

    Preferred over the normal approximation because every interesting cell here is a small
    count near 0 or near 1, where the normal approximation runs outside [0,1] and understates
    uncertainty.
    """
    if hits <= 0:
        return 0.0, 1.0
    denominator = 1.0 + z * z / hits
    centre = (precision + z * z / (2 * hits)) / denominator
    margin = (
        z * ((precision * (1 - precision) / hits + z * z / (4 * hits * hits)) ** 0.5)
    ) / denominator
    return max(centre - margin, 0.0), min(centre + margin, 1.0)


def wilson_lower_bound(hits: int, precision: float, z: float = _WILSON_Z) -> float:
    """Lower bound only — the shrinkage form used for rule weighting in ml/risk.py."""
    if hits <= 0:
        return 0.0
    return wilson_interval(hits, precision, z)[0]


def _empty_cell() -> dict:
    return {"sampled_positive": 0, "sampled_negative": 0}


def rule_of_three_upper_count(stratum_sample_size: int) -> float:
    """Upper 95% bound on how many population members a *zero* observed cell could hide.

    When a stratum contributes no observations to a level, the bootstrap resamples zeros
    forever and reports a degenerate interval — for CRITICAL that came out as a flat
    100%-100%, which reads as certainty when it is really an empty cell. The rule of three
    says an event unobserved in n trials still has a rate as high as 3/n at 95% confidence, so
    the level could hold up to `weight * 3` members of that stratum.
    """
    if stratum_sample_size <= 0:
        return 0.0
    return 3.0


def crosstab(
    records: Iterable[dict],
    stratum_weights: dict[str, float] | None = None,
) -> dict:
    """Cross-tabulate risk level against ground truth.

    `records` are dicts with `risk_level`, `is_laundering` (bool) and `stratum` (a key into
    `stratum_weights`). Records carrying an `error` key are counted separately and excluded
    from the table — a scoring failure is not evidence either way, and folding it into LOW
    would report unexamined accounts as clean, the exact failure mode this workstream has
    already had to fix twice.

    With no weights supplied every observation counts once, which is correct for an
    unstratified sample and makes the function usable in tests without a weighting scheme.
    """
    weights = stratum_weights or {}
    cells: dict[str, dict] = {level: _empty_cell() for level in RISK_ORDER}
    weighted: dict[str, dict[str, float]] = {
        level: {"positive": 0.0, "negative": 0.0} for level in RISK_ORDER
    }
    # Per-stratum counts per level, so a stratum that contributed nothing to a level can be
    # distinguished from one that was never sampled at all.
    per_stratum: dict[str, dict[Any, int]] = {level: {} for level in RISK_ORDER}
    stratum_sizes: dict[Any, int] = {}
    errors = 0
    unknown_levels: dict[str, int] = {}

    for record in records:
        if record.get("error"):
            errors += 1
            continue
        level = record.get("risk_level")
        stratum = record.get("stratum")
        stratum_sizes[stratum] = stratum_sizes.get(stratum, 0) + 1
        if level not in cells:
            unknown_levels[str(level)] = unknown_levels.get(str(level), 0) + 1
            continue
        positive = bool(record.get("is_laundering"))
        weight = float(weights.get(stratum, 1.0))
        cells[level]["sampled_positive" if positive else "sampled_negative"] += 1
        weighted[level]["positive" if positive else "negative"] += weight
        per_stratum[level][stratum] = per_stratum[level].get(stratum, 0) + 1

    levels = {}
    for level in RISK_ORDER:
        sampled_pos = cells[level]["sampled_positive"]
        sampled_neg = cells[level]["sampled_negative"]
        sampled_n = sampled_pos + sampled_neg
        est_pos = weighted[level]["positive"]
        est_neg = weighted[level]["negative"]
        est_n = est_pos + est_neg

        precision = (est_pos / est_n) if est_n > 0 else None

        # Worst-case precision if every stratum that contributed nothing here is actually
        # sitting just under its detection limit. For a level whose false positives are all
        # unobserved, this is the only honest statement available - the point estimate and the
        # bootstrap both say 100% purely because the cell is empty.
        hidden = 0.0
        for stratum, size in stratum_sizes.items():
            if per_stratum[level].get(stratum, 0) == 0:
                hidden += float(weights.get(stratum, 1.0)) * rule_of_three_upper_count(size)
        conservative = (est_pos / (est_n + hidden)) if (est_n + hidden) > 0 else None

        levels[level] = {
            "sampled_n": sampled_n,
            "sampled_positive": sampled_pos,
            "sampled_negative": sampled_neg,
            "estimated_population_n": round(est_n, 1),
            "estimated_positive": round(est_pos, 1),
            "precision": round(precision, 5) if precision is not None else None,
            "precision_conservative": (
                round(conservative, 5) if conservative is not None else None
            ),
            "unobserved_strata": sorted(
                str(s) for s in stratum_sizes if per_stratum[level].get(s, 0) == 0
            ),
            # Filled in by attach_bootstrap_ci. Not a Wilson interval on the sampled
            # proportion: that would describe a different quantity than the weighted
            # precision beside it (for a rare level the two differ by an order of magnitude,
            # which reads as a typo rather than as two statistics).
            "precision_ci95": None,
            "noisy": sampled_n < MIN_CELL_FOR_STABLE_ESTIMATE,
        }

    total_pos = sum(w["positive"] for w in weighted.values())
    total_n = sum(w["positive"] + w["negative"] for w in weighted.values())
    base_rate = (total_pos / total_n) if total_n > 0 else None

    for level, row in levels.items():
        precision = row["precision"]
        row["lift"] = (
            round(precision / base_rate, 2)
            if precision is not None and base_rate not in (None, 0)
            else None
        )
        # Share of all laundering accounts that land at this level. Recall at CRITICAL alone
        # is the number that says whether a strict threshold is throwing away the cases it
        # exists to catch.
        row["recall"] = round(weighted[level]["positive"] / total_pos, 5) if total_pos else None

    return {
        "levels": levels,
        "base_rate": round(base_rate, 6) if base_rate is not None else None,
        "scored_accounts": sum(c["sampled_positive"] + c["sampled_negative"]
                               for c in cells.values()),
        "errors": errors,
        "unknown_levels": unknown_levels,
    }


def recall_at_or_above(table: dict, level: str) -> float:
    """Share of laundering accounts landing at `level` or higher."""
    if level not in RISK_ORDER:
        raise ValueError(f"unknown level {level!r}")
    cutoff = RISK_ORDER.index(level)
    return sum(
        table["levels"][name]["recall"] or 0.0
        for name in RISK_ORDER[cutoff:]
    )


def bootstrap_precision_ci(
    records: Sequence[dict],
    stratum_weights: dict[str, float],
    iterations: int = 400,
    seed: int = 42,
) -> dict[str, list[float] | None]:
    """Percentile CIs for every level's weighted precision, resampling within each stratum.

    A Wilson interval would treat a level's cell as a single binomial sample and ignore that
    the weighting amplifies a handful of clean-stratum observations into hundreds of estimated
    false positives. Resampling the actual sample reflects that amplification, which for the
    rare levels is the dominant source of uncertainty.

    All four levels come out of one set of resamples — they are computed from the same draws,
    so running the loop per level would cost four times as much and produce inconsistent
    intervals.
    """
    import random

    by_stratum: dict[Any, list[dict]] = {}
    for record in records:
        if record.get("error"):
            continue
        by_stratum.setdefault(record.get("stratum"), []).append(record)
    if not by_stratum:
        return {level: None for level in RISK_ORDER}

    rng = random.Random(seed)
    estimates: dict[str, list[float]] = {level: [] for level in RISK_ORDER}
    for _ in range(iterations):
        resampled: list[dict] = []
        for stratum_records in by_stratum.values():
            resampled.extend(
                rng.choice(stratum_records) for _ in range(len(stratum_records))
            )
        table = crosstab(resampled, stratum_weights)
        for level in RISK_ORDER:
            precision = table["levels"][level]["precision"]
            if precision is not None:
                estimates[level].append(precision)

    intervals: dict[str, list[float] | None] = {}
    for level, values in estimates.items():
        if not values:
            intervals[level] = None
            continue
        values.sort()
        intervals[level] = [
            round(values[int(0.025 * (len(values) - 1))], 5),
            round(values[int(0.975 * (len(values) - 1))], 5),
        ]
    return intervals


def attach_bootstrap_ci(
    table: dict,
    records: Sequence[dict],
    stratum_weights: dict[str, float],
    iterations: int = 400,
    seed: int = 42,
) -> dict:
    """Fill each level's `precision_ci95` in place and return the table."""
    intervals = bootstrap_precision_ci(records, stratum_weights, iterations, seed)
    for level, interval in intervals.items():
        table["levels"][level]["precision_ci95"] = interval
    return table


def format_markdown(table: dict) -> str:
    """Render the cross-tab as a Markdown table matching the RULE_STATS style in ml_spec.md."""
    lines = [
        "| Risk level | Sampled n | Sampled launderers | Est. population n | Precision "
        "| Worst case | 95% CI | Lift | Recall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for level in RISK_ORDER:
        row = table["levels"][level]
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.1%}"
        if row["noisy"]:
            # ASCII only: this gets printed to a Windows console under cp1252.
            precision += " (noisy)"
        worst = row.get("precision_conservative")
        worst_text = "n/a" if worst is None else f"{worst:.1%}"
        lift = "n/a" if row["lift"] is None else f"{row['lift']:.1f}x"
        recall = "n/a" if row["recall"] is None else f"{row['recall']:.1%}"
        interval = row.get("precision_ci95")
        ci = "n/a" if not interval else f"{interval[0]:.1%}-{interval[1]:.1%}"
        # A bootstrap over a cell no stratum reached collapses to a single value; saying so
        # beats printing "100.0%-100.0%" as though it were a measurement.
        if row.get("unobserved_strata") and interval and interval[0] == interval[1]:
            ci += " (empty cell)"
        lines.append(
            f"| {level} | {row['sampled_n']:,} | {row['sampled_positive']:,} "
            f"| {row['estimated_population_n']:,.0f} | {precision} | {worst_text} "
            f"| {ci} | {lift} | {recall} |"
        )
    return "\n".join(lines)
