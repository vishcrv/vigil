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

# --- Rule weights, from the measured lift table in phase4.md §3 ---------------------------
# Base rate is 0.75% of accounts, so "lift" is precision / 0.0075. Weights are assigned by
# band rather than by a formula on lift, because lift alone is noisy at low hit counts
# (BIPARTITE's 8.9x rests on 15 accounts) and because what matters downstream is the coarse
# question "is this rule evidence, a hint, or noise".
RULE_WEIGHTS: dict[str, float] = {
    "SCATTER-GATHER": 1.00,   # 100% precision, 133x lift  - strongest available evidence
    "GATHER-SCATTER": 0.85,   #  52% precision,  70x lift
    "RANDOM":         0.35,   # 8.7% precision, 11.6x lift
    "BIPARTITE":      0.30,   # 6.7% precision,  8.9x lift (only 15 accounts - thin)
    "FAN-OUT":        0.25,   # 5.0% precision,  6.7x lift
    "CYCLE":          0.15,   # 2.8% precision,  3.7x lift
    "STACK":          0.15,   # 2.7% precision,  3.6x lift (best recall, weak precision)
    "FAN-IN":         0.05,   # 1.1% precision,  1.5x lift - near chance, deliberately minimal
}

# Firing at all is most of a rule's evidentiary value; the rule's own confidence (how far past
# threshold it went) supplies the rest. Without this floor, a rule that fires just over its
# threshold contributes almost nothing even when it is a 100%-precision rule.
RULE_BASE_CREDIT = 0.60

# Detectors are far weaker than the good rules (Isolation Forest 2.3x lift vs 133x), so they
# can raise a score but never carry one to CRITICAL alone. At D=1.0 with no rule hit the
# combined score reaches exactly this value.
DETECTOR_WEIGHT = 0.40

# phase4.md §7: the top-scoring row for a sample account was a large routine self-transfer in
# a payment format measured at a 0.0% laundering rate. Amount-driven features score those
# highly, so damp the detector when the scored rows are dominated by that profile.
BENIGN_FORMAT_RISK_CEILING = 1e-9
BENIGN_DETECTOR_DAMPING = 0.50

# ACH measured 0.75% laundering rate against 0.038% for the next format (phase2.md §4), so
# anything at or above this floor is the over-represented end of the distribution.
ELEVATED_FORMAT_RISK_FLOOR = 0.005

RISK_THRESHOLDS = [
    ("CRITICAL", 0.75),
    ("HIGH", 0.50),
    ("MEDIUM", 0.25),
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


def _level_for(score: float) -> str:
    for level, floor_val in RISK_THRESHOLDS:
        if score >= floor_val:
            return level
    return "LOW"


def _rule_component(rule_hits: list) -> tuple[float, str | None, list[dict]]:
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
        contribution = weight * (RULE_BASE_CREDIT + (1.0 - RULE_BASE_CREDIT) * confidence)
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


def risk(anomaly_result: dict, context: dict | None = None) -> dict:
    if not isinstance(anomaly_result, dict):
        return {"error": "anomaly_result must be a dict"}
    if context is not None and not isinstance(context, dict):
        return {"error": "context must be a dict when provided"}
    # A dict that is not an anomaly result would otherwise fall through to the "nothing
    # matched" branch and be reported as LOW / MONITOR - i.e. a caller who passed the wrong
    # object gets told the account is clean. Require the one key every success path sets.
    if "error" not in anomaly_result and "row_count_scored" not in anomaly_result:
        return {
            "error": "anomaly_result does not look like an anomaly() result "
                     "(no 'row_count_scored' key)",
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
                "pattern_detected": None,
                "anomaly_score": 0.0,
                "escalation_action": "MONITOR",
                "customer_id": context.get("customer_id")
                or (anomaly_result.get("scope") or {}).get("account_id"),
                "transaction_id": None,
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

        rule_score, top_rule, rule_breakdown = _rule_component(subject_hits)
        # Deliberately does not feed the score: transacting with a flagged account is
        # meaningful in AML, but no weight for it was measured in Phase 4 and inventing one
        # would be a guess. Surfaced so `explain` can say it and a judge can follow it up.
        _, _, counterparty_breakdown = _rule_component(counterparty_hits)

        benign_fraction = _benign_fraction(top_rows)
        adjusted_detector = detector_score * (1.0 - BENIGN_DETECTOR_DAMPING * benign_fraction)

        # Rules lead; the detector fills part of the remaining headroom. Multiplying into
        # (1 - rule_score) keeps the result in [0,1] and stops a strong rule plus a strong
        # detector from summing past the top of the scale.
        combined = rule_score + (1.0 - rule_score) * adjusted_detector * DETECTOR_WEIGHT
        combined = min(max(combined, 0.0), 1.0)
        level = _level_for(combined)

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
