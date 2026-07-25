"""
Phase 5 — `risk(anomaly_result, context?) -> dict`.

Maps Phase 4's two signals (continuous detector score + named rule hits) onto a discrete risk
level and a recommended action, in exactly the shape a `flags` row takes (`spec.md` line
177-184).

The weighting is not arbitrary: each rule's weight is set from the precision/lift it actually
achieved against pattern-file ground truth in Phase 4 (`phase4.md` §3). A rule that hit 100%
precision and a rule barely better than chance must not contribute equally, and encoding that
here is the whole reason Phase 4 kept the signals separate instead of blending them early.
"""
from typing import Any

from ml.validation import wilson_lower_bound

# --- Rule weights, derived from the measured table in phase4.md §3 ------------------------
# (accounts hit, precision) per rule. Both numbers matter: the hit count is what says how much
# of the precision to believe. BIPARTITE's headline 6.7% rests on 15 accounts and STACK's 2.7%
# on 8,858, and an earlier hand-banded weight table treated those as comparable evidence.
RULE_STATS: dict[str, tuple[int, float]] = {
    "SCATTER-GATHER": (306, 1.000),
    "GATHER-SCATTER": (46, 0.522),
    "RANDOM": (12, 0.087),
    "BIPARTITE": (15, 0.067),
    "FAN-OUT": (1736, 0.050),
    "CYCLE": (1249, 0.028),
    "STACK": (8858, 0.027),
    "FAN-IN": (3050, 0.011),
}

# phase4.md §3: 3,170 implicated accounts of 422,726 with a non-self-loop edge.
ACCOUNT_BASE_RATE = 3170 / 422726

# 95% two-sided.
_WILSON_Z = 1.96


def _wilson_lower_bound(hits: int, precision: float, z: float = _WILSON_Z) -> float:
    """Lower bound of the Wilson score interval for an observed precision.

    Shrinks a rule's credit toward zero in proportion to how little was measured, so a rule
    seen 15 times cannot outrank one seen 1,736 times on the strength of a noisier point
    estimate. Shares one implementation with the validation cross-tab, which needs the full
    interval.
    """
    return wilson_lower_bound(hits, precision, z)


def _derive_rule_weights() -> dict[str, float]:
    """Turn (hits, precision) into a [0,1] weight per rule.

    Log-scaled in lift rather than linear: linear in lift would give SCATTER-GATHER's 130x
    weight 1.0 and collapse every other rule to under 0.05, which throws away the ordering
    among the mid-tier rules that a triage queue actually needs. Normalized so the
    best-evidenced rule is exactly 1.0 — the scale is relative to the best rule available, not
    to an absolute notion of certainty.
    """
    import math

    lifts = {
        rule: _wilson_lower_bound(hits, precision) / ACCOUNT_BASE_RATE
        for rule, (hits, precision) in RULE_STATS.items()
    }
    logs = {rule: math.log(lift) if lift > 1.0 else 0.0 for rule, lift in lifts.items()}
    best = max(logs.values())
    if best <= 0:
        return {rule: 0.0 for rule in RULE_STATS}
    return {rule: round(value / best, 3) for rule, value in logs.items()}


RULE_WEIGHTS: dict[str, float] = _derive_rule_weights()

# Firing at all is most of a rule's evidentiary value; the rule's own confidence (how far past
# threshold it went) supplies the rest. Without this floor, a rule that fires just over its
# threshold contributes almost nothing even when it is a 100%-precision rule.
# Tuned (scripts/tune_blend.py): 0.60 -> 0.40. A lower floor spreads rule-driven scores out
# instead of bunching every hit near its rule's weight, which is what let MEDIUM and HIGH
# separate.
RULE_BASE_CREDIT = 0.40

# Detectors are far weaker than the good rules (Isolation Forest 2.3x lift vs 133x), so they
# can raise a score but never carry one to CRITICAL alone. At D=1.0 with no rule hit the
# combined score reaches exactly this value.
# Tuned (scripts/tune_blend.py): 0.40 -> 0.30.
DETECTOR_WEIGHT = 0.30

# phase4.md §7: the top-scoring row for a sample account was a large routine self-transfer in
# a payment format measured at a 0.0% laundering rate. Amount-driven features score those
# highly, so damp the detector when the scored rows are dominated by that profile.
BENIGN_FORMAT_RISK_CEILING = 1e-9
# Tuned (scripts/tune_blend.py): 0.50 -> 0.25.
BENIGN_DETECTOR_DAMPING = 0.25

# ACH measured 0.75% laundering rate against 0.038% for the next format (phase2.md §4), so
# anything at or above this floor is the over-represented end of the distribution.
ELEVATED_FORMAT_RISK_FLOOR = 0.005

# Tuned against the validated sample (scripts/tune_blend.py, ml_spec.md "Blend tuning"):
# was CRITICAL 0.75 / HIGH 0.50 / MEDIUM 0.25. The old floors put 62% of all accounts in
# MEDIUM and left HIGH holding 0.5%; these split that bucket so HIGH+ recall goes 7.2% -> 66%
# with CRITICAL's precision unchanged.
RISK_THRESHOLDS = [
    ("CRITICAL", 0.55),
    ("HIGH", 0.25),
    ("MEDIUM", 0.20),
    ("LOW", 0.0),
]

# ml_spec.md open decision #1, resolved: four risk tiers onto the three actions spec.md line
# 182 defines. HIGH and MEDIUM share REVIEW - the tiers still differ in queue ordering.
ESCALATION_ACTIONS = {
    "LOW": "MONITOR",
    "MEDIUM": "REVIEW",
    "HIGH": "REVIEW",
    "CRITICAL": "REPORT",
}

RISK_LEVELS = [level for level, _ in RISK_THRESHOLDS]

# Emitted when no named rule fired. A string rather than None because `FlaggedItem`
# (backend/schemas.py) types `pattern_detected` as a required non-nullable str: a null
# there fails validation and agent/loop.py drops the whole flag without a word. Detector-
# driven flags routinely have no motif, so this is the common case, not an edge one.
# `explain.select_template` tests membership against MOTIF_TEMPLATES, so this falls through
# to the pure-anomaly template exactly as None did.
NO_PATTERN = "NONE"

# Same thresholds keyed by level, for callers that need to look one up rather than walk the
# ordered list (the tuning sweep evaluates floors directly).
RISK_ORDER_FLOORS: dict[str, float] = {level: floor for level, floor in RISK_THRESHOLDS}


# The four constants above plus the thresholds, bundled so they can be varied without
# reassigning module globals. `scripts/tune_blend.py` sweeps this; nothing else passes it, so
# the default path is byte-for-byte the previous behaviour. Deliberately not exposed in the
# tool schema — the agent has no business retuning the risk model mid-query.
DEFAULT_BLEND: dict[str, Any] = {
    "detector_weight": DETECTOR_WEIGHT,
    "rule_base_credit": RULE_BASE_CREDIT,
    "benign_damping": BENIGN_DETECTOR_DAMPING,
    "thresholds": RISK_THRESHOLDS,
}


def _level_for(score: float, thresholds: list | None = None) -> str:
    for level, floor_val in (thresholds or RISK_THRESHOLDS):
        if score >= floor_val:
            return level
    return "LOW"


def _rule_component(
    rule_hits: list, base_credit: float = RULE_BASE_CREDIT
) -> tuple[float, str | None, list[dict]]:
    """Strongest weighted rule hit, plus a per-rule breakdown for `explain`.

    Uses the maximum rather than a sum: two weak rules firing on the same account is not
    stronger evidence than one 100%-precision rule, and summing would let a pile of
    low-precision hits manufacture a CRITICAL.
    """
    scored = []
    for hit in rule_hits:
        if not isinstance(hit, dict):
            continue
        name = hit.get("rule")
        weight = RULE_WEIGHTS.get(name)
        if weight is None:
            continue
        confidence = hit.get("score")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        confidence = min(max(confidence, 0.0), 1.0)
        contribution = weight * (base_credit + (1.0 - base_credit) * confidence)
        scored.append({
            "rule": name,
            "account": hit.get("account"),
            "weight": weight,
            "rule_confidence": round(confidence, 4),
            "contribution": round(contribution, 4),
            "evidence": hit.get("evidence"),
        })

    if not scored:
        return 0.0, None, []
    scored.sort(key=lambda s: -s["contribution"])
    return scored[0]["contribution"], scored[0]["rule"], scored


def _benign_fraction(top_rows: list) -> float:
    """Share of scored rows that are self-transfers in a zero-laundering-risk payment format."""
    usable = [r for r in top_rows if isinstance(r, dict)]
    if not usable:
        return 0.0
    benign = sum(
        1 for r in usable
        if r.get("is_self_loop") is True
        and isinstance(r.get("payment_format_risk"), (int, float))
        and r["payment_format_risk"] <= BENIGN_FORMAT_RISK_CEILING
    )
    return benign / len(usable)


def _flag_fractions(top_rows: list) -> dict[str, float]:
    """Descriptive row-level flags: currency mismatch and elevated payment-format risk.

    These do **not** feed the score. Neither was measured as an independent signal in Phase 4,
    so weighting them here would be an unmeasured guess. They are surfaced because Phase 6
    needs a citable reason when no rule fired — `ml_spec.md` requires an explanation path for
    currency-mismatch / payment-format-only flags, and that path needs real values to quote.
    """
    usable = [r for r in top_rows if isinstance(r, dict)]
    if not usable:
        return {"currency_mismatch": 0.0, "elevated_payment_format": 0.0}
    mismatch = sum(1 for r in usable if r.get("currency_mismatch") is True)
    elevated = sum(
        1 for r in usable
        if isinstance(r.get("payment_format_risk"), (int, float))
        and r["payment_format_risk"] >= ELEVATED_FORMAT_RISK_FLOOR
    )
    return {
        "currency_mismatch": mismatch / len(usable),
        "elevated_payment_format": elevated / len(usable),
    }


def risk(
    anomaly_result: dict | None = None,
    context: dict | None = None,
    blend: dict | None = None,
    scope: dict | None = None,
) -> dict:
    if context is not None and not isinstance(context, dict):
        return {"error": "context must be a dict when provided"}

    # `scope` is the recovery path for the hand-off, not an alternative API. The model has to
    # copy anomaly()'s whole result into this call, and when it paraphrases or drops fields
    # instead, the strict check below would return an error and the run would end with zero
    # flags and no obvious cause. Re-deriving from the scope costs nothing — anomaly() is
    # memoised, so this is a cache hit on the call the model just made.
    looks_valid = isinstance(anomaly_result, dict) and (
        "row_count_scored" in anomaly_result or "error" in anomaly_result
    )
    if not looks_valid and isinstance(scope, dict) and scope:
        from ml.anomaly import anomaly as _anomaly  # local import: ml.anomaly imports risk's peers

        anomaly_result = _anomaly(scope)

    if not isinstance(anomaly_result, dict):
        return {"error": "risk needs anomaly_result (the full dict from anomaly) or scope"}
    # A dict that is not an anomaly result would otherwise fall through to the "nothing
    # matched" branch and be reported as LOW / MONITOR - i.e. a caller who passed the wrong
    # object gets told the account is clean. Require the one key every success path sets.
    if "error" not in anomaly_result and "row_count_scored" not in anomaly_result:
        return {
            "error": "anomaly_result does not look like an anomaly() result "
                     "(no 'row_count_scored' key); pass the full dict anomaly returned, "
                     "or pass scope instead",
            "risk_level": None,
            "escalation_action": None,
        }
    if "error" in anomaly_result:
        # Propagate rather than scoring a failed detection as LOW risk, which would read as
        # "we checked and it is fine" when nothing was actually checked.
        return {
            "error": f"upstream anomaly error: {anomaly_result['error']}",
            "risk_level": None,
            "escalation_action": None,
        }

    try:
        context = context or {}
        settings = {**DEFAULT_BLEND, **(blend or {})}
        detector_weight = settings["detector_weight"]
        base_credit = settings["rule_base_credit"]
        benign_damping = settings["benign_damping"]
        thresholds = settings["thresholds"]
        rule_hits = anomaly_result.get("rule_hits") or []
        top_rows = anomaly_result.get("top_rows") or []
        rows_scored = anomaly_result.get("row_count_scored") or 0

        detector_score = anomaly_result.get("anomaly_score")
        detector_score = (
            float(detector_score) if isinstance(detector_score, (int, float)) else 0.0
        )
        detector_score = min(max(detector_score, 0.0), 1.0)

        if rows_scored == 0 and not rule_hits:
            return {
                "risk_level": "LOW",
                "risk_score": 0.0,
                "pattern_detected": NO_PATTERN,
                "anomaly_score": 0.0,
                "escalation_action": "MONITOR",
                "customer_id": context.get("customer_id")
                or (anomaly_result.get("scope") or {}).get("account_id"),
                "transaction_id": None,
                "amount": 0.0,
                "timestamp": None,
                "contributing_signals": {"note": "no transactions matched this scope"},
            }

        # anomaly() returns rule hits for every account appearing in the scoped rows, which
        # includes counterparties. Attributing a counterparty's motif to the subject account
        # would write a factually wrong flags row ("this account fans out" when its payee
        # does). When the scope names an account, only that account's hits drive the score
        # and pattern_detected; the rest are reported as network context.
        subject = (anomaly_result.get("scope") or {}).get("account_id")
        if subject:
            subject_hits = [
                h for h in rule_hits if isinstance(h, dict) and h.get("account") == subject
            ]
            counterparty_hits = [
                h for h in rule_hits if isinstance(h, dict) and h.get("account") != subject
            ]
        else:
            subject_hits, counterparty_hits = rule_hits, []

        rule_score, top_rule, rule_breakdown = _rule_component(subject_hits, base_credit)
        top_rule = top_rule or NO_PATTERN
        # Deliberately does not feed the score: transacting with a flagged account is
        # meaningful in AML, but no weight for it was measured in Phase 4 and inventing one
        # would be a guess. Surfaced so `explain` can say it and a judge can follow it up.
        _, _, counterparty_breakdown = _rule_component(counterparty_hits, base_credit)

        benign_fraction = _benign_fraction(top_rows)
        adjusted_detector = detector_score * (1.0 - benign_damping * benign_fraction)

        # Rules lead; the detector fills part of the remaining headroom. Multiplying into
        # (1 - rule_score) keeps the result in [0,1] and stops a strong rule plus a strong
        # detector from summing past the top of the scale.
        combined = rule_score + (1.0 - rule_score) * adjusted_detector * detector_weight
        combined = min(max(combined, 0.0), 1.0)
        level = _level_for(combined, thresholds)

        top_row = top_rows[0] if top_rows and isinstance(top_rows[0], dict) else {}
        customer_id = (
            context.get("customer_id")
            or (anomaly_result.get("scope") or {}).get("account_id")
            or top_row.get("From Account")
        )

        return {
            # --- flags-table fields (spec.md line 177-184) ---
            "risk_level": level,
            "pattern_detected": top_rule,
            "anomaly_score": round(detector_score, 4),
            "escalation_action": ESCALATION_ACTIONS[level],
            "customer_id": customer_id,
            "transaction_id": _transaction_ref(top_row),
            # `FlaggedItem` (backend/schemas.py) requires both, and the frontend's table renders
            # amount while the temporal chart plots timestamp. Emitted here rather than left for
            # the model to scavenge out of `anomaly`'s top_rows: an item missing either field
            # fails Pydantic validation and is silently dropped by the loop, so the flag would
            # vanish with no error anywhere.
            "amount": _amount_of(top_row),
            "timestamp": _timestamp_of(top_row),
            # --- supporting detail, for Phase 6 and the transparency panel ---
            "risk_score": round(combined, 4),
            "contributing_signals": {
                "rule_component": round(rule_score, 4),
                "detector_component_raw": round(detector_score, 4),
                "detector_component_adjusted": round(adjusted_detector, 4),
                "benign_profile_fraction": round(benign_fraction, 4),
                "rules_fired": rule_breakdown,
                "counterparty_rules": counterparty_breakdown,
                "rows_scored": rows_scored,
                # Carried through so a judge reading the flag can see the score covers the
                # most recent slice of a larger scope, not all of it.
                "rows_matched": anomaly_result.get("rows_matched", rows_scored),
                "scope_truncated": bool(anomaly_result.get("truncated", False)),
                # Descriptive only - see _flag_fractions. Phase 6 quotes these.
                "row_flags": {k: round(v, 4) for k, v in _flag_fractions(top_rows).items()},
            },
        }
    except Exception as exc:
        return {"error": str(exc), "risk_level": None, "escalation_action": None}


def _amount_of(row: dict) -> float:
    """Amount of the highest-scoring transaction. 0.0 rather than None when absent — the
    schema types this float, and a null would fail validation and drop the whole flag."""
    value = row.get("Amount Received")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _timestamp_of(row: dict) -> str | None:
    """Transaction timestamp as ISO 8601, which is what `FlaggedItem.timestamp` parses.

    Stored form is the fixed-width VARCHAR `YYYY/MM/DD HH:MM` (ml/dates.py), and Pydantic
    rejects that, so the separators are swapped and a seconds field added here.
    """
    raw = row.get("Timestamp")
    if not isinstance(raw, str) or len(raw) < 16:
        return None
    date_part, _, time_part = raw.partition(" ")
    return f"{date_part.replace('/', '-')}T{time_part}:00"


def _transaction_ref(row: dict) -> str | None:
    """Deterministic reference for the highest-scoring transaction.

    The Kaggle data has no transaction ID (phase1.md §9 - pattern rows are matched back by
    full field tuple, not a key), but teammate's `flags` table has a `transaction_id` column.
    A stable hash of the identifying tuple fills it without inventing a fake sequential ID
    that would not survive a re-run of the enrichment pipeline.
    """
    import hashlib

    parts = [
        row.get("Timestamp"), row.get("From Account"),
        row.get("To Account"), row.get("Amount Received"),
    ]
    if any(p is None for p in parts):
        return None
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return f"tx_{digest[:16]}"
