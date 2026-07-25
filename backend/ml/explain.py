"""
Phase 6 — `explain(risk_result) -> dict`.

Template-based natural language tied to the signal that actually fired, per `spec.md`'s
"Explain" row (no SHAP/LIME). One template per motif, filled with the real values the rule
recorded in its evidence JSON, plus templates for the non-rule paths.

Coverage requirement from `ml_spec.md` Phase 6: every possible `risk()` output must land on a
template — there is no silent "no explanation available" case. `select_template()` is total
over the possible shapes and `tests/test_explain.py` asserts that.
"""
from typing import Any, Callable

# Detector percentile at or above which a scored row is worth narrating on its own, with no
# rule behind it. 0.95 keeps the pure-anomaly template for genuine tail cases rather than
# letting it narrate ordinary traffic.
PURE_ANOMALY_FLOOR = 0.95

# Any flag fraction at or above this is worth a sentence.
FLAG_MENTION_FLOOR = 0.20


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "an unrecorded amount"


def _num(value: Any, fallback: str = "an unrecorded number of") -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return fallback


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "an unrecorded share"


def _when(evidence: dict, key: str) -> str:
    val = evidence.get(key)
    return str(val) if val else "an unrecorded time"


# --- Motif templates ----------------------------------------------------------------------
# One per `aml_pattern` value from phase1.md §9. Each quotes the fields its own rule wrote
# into `evidence` (see ml/rules.py), so the numbers in the sentence are the numbers that
# triggered the rule — not a restatement of the score.

def _fan_out(account: str, ev: dict) -> str:
    return (
        f"Account {account} sent funds to {_num(ev.get('unique_receivers'))} distinct new "
        f"receivers within a {_num(ev.get('window_hours'), 'short')}-hour window starting "
        f"{_when(ev, 'window_start')}, moving {_money(ev.get('window_amount'))} in total. "
        f"A burst of new counterparties in a short window is consistent with a FAN-OUT "
        f"distribution pattern, where one account disperses funds across many recipients."
    )


def _fan_in(account: str, ev: dict) -> str:
    return (
        f"Account {account} received funds from {_num(ev.get('unique_senders'))} distinct new "
        f"senders within a {_num(ev.get('window_hours'), 'short')}-hour window starting "
        f"{_when(ev, 'window_start')}, totalling {_money(ev.get('window_amount'))}. "
        f"Many sources converging on one account in a short window is consistent with a "
        f"FAN-IN consolidation pattern. Note this rule is weakly discriminating on this "
        f"dataset and should not be relied on alone."
    )


def _cycle(account: str, ev: dict) -> str:
    return (
        f"Funds left account {account} and returned to it through a "
        f"{_num(ev.get('hops'), 'short')}-hop loop: {_money(ev.get('out_amount'))} out at "
        f"{_when(ev, 'started_at')}, {_money(ev.get('returned_amount'))} back by "
        f"{_when(ev, 'closed_at')} — {_pct(ev.get('retention_pct'))} of the original value "
        f"retained across {_num(ev.get('cycles_found'), 'multiple')} such loop(s). Value "
        f"returning to its origin after intermediate hops is consistent with a CYCLE pattern."
    )


def _stack(account: str, ev: dict) -> str:
    return (
        f"Account {account} is part of a {_num(ev.get('hops'), 'multi')}-hop pass-through "
        f"chain in which each hop forwarded most of what it received: "
        f"{_money(ev.get('amount_in'))} entering at {_when(ev, 'started_at')} and "
        f"{_money(ev.get('amount_out'))} leaving by {_when(ev, 'ended_at')} "
        f"({_pct(ev.get('retention_pct'))} retained), across "
        f"{_num(ev.get('chains_found'), 'multiple')} such chain(s). Consecutive transfers that "
        f"preserve value are consistent with a STACK layering pattern."
    )


def _gather_scatter(account: str, ev: dict) -> str:
    return (
        f"Account {account} collected funds from {_num(ev.get('unique_senders'))} distinct "
        f"senders starting {_when(ev, 'first_inflow')} "
        f"({_money(ev.get('amount_in'))}), then redistributed to "
        f"{_num(ev.get('unique_receivers'))} distinct receivers by "
        f"{_when(ev, 'last_outflow')} ({_money(ev.get('amount_out'))}, "
        f"{_pct(ev.get('passthrough_pct'))} passed through). Collection followed by "
        f"redistribution is consistent with a GATHER-SCATTER pattern typical of a mule account."
    )


def _scatter_gather(account: str, ev: dict) -> str:
    return (
        f"Account {account} is part of a structure in which funds split across "
        f"{_num(ev.get('intermediaries'))} distinct intermediary accounts and reconverged on a "
        f"single destination, between {_when(ev, 'started_at')} and {_when(ev, 'ended_at')} "
        f"({_money(ev.get('amount_in'))} in, {_money(ev.get('amount_out'))} out). Parallel "
        f"paths between one origin and one endpoint are consistent with a SCATTER-GATHER "
        f"layering pattern. This is the highest-precision rule in the detection set."
    )


def _bipartite(account: str, ev: dict) -> str:
    return (
        f"Account {account} belongs to a dense block of "
        f"{_num(ev.get('block_senders'))} senders paying into a shared set of "
        f"{_num(ev.get('shared_receivers'))} common receivers. Tightly overlapping "
        f"counterparty sets across otherwise unrelated senders are consistent with a "
        f"BIPARTITE pattern."
    )


def _random(account: str, ev: dict) -> str:
    return (
        f"Account {account} shows unusually high activity "
        f"({_num(ev.get('txn_count'))} transactions) in both directions without matching any "
        f"of the structured motifs. The pattern set includes a RANDOM category for laundering "
        f"attempts with no clean topology, and this account fits that residual profile."
    )


MOTIF_TEMPLATES: dict[str, Callable[[str, dict], str]] = {
    "FAN-OUT": _fan_out,
    "FAN-IN": _fan_in,
    "CYCLE": _cycle,
    "STACK": _stack,
    "GATHER-SCATTER": _gather_scatter,
    "SCATTER-GATHER": _scatter_gather,
    "BIPARTITE": _bipartite,
    "RANDOM": _random,
}

NON_MOTIF_TEMPLATES = [
    "ERROR", "NO_DATA", "PURE_ANOMALY", "ROW_FLAGS_ONLY", "NO_CONCERN",
]

ALL_TEMPLATES = sorted(MOTIF_TEMPLATES) + NON_MOTIF_TEMPLATES


def _detector_sentence(signals: dict) -> str:
    raw = signals.get("detector_component_raw")
    adjusted = signals.get("detector_component_adjusted")
    rows = signals.get("rows_scored")
    try:
        raw_f = float(raw)
    except (TypeError, ValueError):
        return ""
    sentence = (
        f" Statistical detectors scored the most unusual of "
        f"{_num(rows, 'the')} scored transactions at the "
        f"{raw_f * 100:.1f}th percentile of the reference population."
    )
    try:
        if float(adjusted) < raw_f - 1e-9:
            benign = signals.get("benign_profile_fraction")
            sentence += (
                f" That score was reduced to {float(adjusted) * 100:.1f}% because "
                f"{_pct(float(benign) * 100)} of the scored rows are self-transfers in a "
                f"payment format with no observed laundering in this dataset."
            )
    except (TypeError, ValueError):
        pass
    return sentence


def _flag_sentence(signals: dict) -> str:
    flags = signals.get("row_flags") or {}
    parts = []
    try:
        if float(flags.get("currency_mismatch", 0)) >= FLAG_MENTION_FLOOR:
            parts.append(
                f"{_pct(float(flags['currency_mismatch']) * 100)} of scored transactions "
                f"convert between currencies"
            )
    except (TypeError, ValueError):
        pass
    try:
        if float(flags.get("elevated_payment_format", 0)) >= FLAG_MENTION_FLOOR:
            parts.append(
                f"{_pct(float(flags['elevated_payment_format']) * 100)} use a payment format "
                f"over-represented among known laundering activity"
            )
    except (TypeError, ValueError):
        pass
    if not parts:
        return ""
    return " Contributing context: " + " and ".join(parts) + "."


def _counterparty_sentence(signals: dict) -> str:
    """Motifs matched by counterparties, not by the subject account.

    Reported separately and never attributed to the subject — see the split in ml/risk.py.
    Suppressing it entirely would drop a real lead; merging it in would misstate whose
    behaviour was detected.
    """
    hits = signals.get("counterparty_rules") or []
    named: dict[str, set] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        rule, account = hit.get("rule"), hit.get("account")
        if rule:
            named.setdefault(str(rule), set())
            if account:
                named[str(rule)].add(str(account))
    if not named:
        return ""
    parts = []
    for rule in sorted(named):
        accounts = sorted(named[rule])
        if len(accounts) == 1:
            parts.append(f"{accounts[0]} ({rule})")
        elif accounts:
            parts.append(f"{len(accounts)} accounts ({rule})")
        else:
            parts.append(rule)
    return (
        " Separately, counterparties of this account matched laundering patterns: "
        + "; ".join(parts)
        + ". These are properties of those accounts, not of the one assessed here, and do not "
        "contribute to the score above."
    )


def _action_sentence(risk_level: Any, action: Any, score: Any) -> str:
    try:
        score_txt = f" (composite risk score {float(score):.2f})"
    except (TypeError, ValueError):
        score_txt = ""
    return f" Assessed risk: {risk_level}{score_txt}. Recommended action: {action}."


def select_template(risk_result: dict) -> str:
    """Which template applies. Total over every shape `risk()` can return."""
    if not isinstance(risk_result, dict):
        return "ERROR"
    if "error" in risk_result or risk_result.get("risk_level") is None:
        return "ERROR"

    signals = risk_result.get("contributing_signals") or {}
    if not signals.get("rows_scored"):
        return "NO_DATA"

    pattern = risk_result.get("pattern_detected")
    if pattern in MOTIF_TEMPLATES:
        return pattern

    try:
        detector = float(signals.get("detector_component_adjusted", 0.0))
    except (TypeError, ValueError):
        detector = 0.0
    if detector >= PURE_ANOMALY_FLOOR:
        return "PURE_ANOMALY"

    flags = signals.get("row_flags") or {}
    try:
        if max(float(v) for v in flags.values()) >= FLAG_MENTION_FLOOR:
            return "ROW_FLAGS_ONLY"
    except (TypeError, ValueError):
        pass

    return "NO_CONCERN"


def explain(risk_result: dict) -> dict:
    try:
        template = select_template(risk_result)

        if template == "ERROR":
            reason = "unknown error"
            if isinstance(risk_result, dict):
                reason = risk_result.get("error") or "risk classification returned no level"
            return {
                "explanation": (
                    f"No assessment could be produced: {reason}. This is a failure to analyse, "
                    f"not a finding that the activity is clean — the transactions in scope have "
                    f"not been evaluated."
                ),
                "template": "ERROR",
            }

        signals = risk_result.get("contributing_signals") or {}
        level = risk_result.get("risk_level")
        action = risk_result.get("escalation_action")
        score = risk_result.get("risk_score")
        account = risk_result.get("customer_id") or "the account in scope"

        if template == "NO_DATA":
            return {
                "explanation": (
                    f"No transactions matched the requested scope, so there is nothing to "
                    f"assess for {account}."
                    + _action_sentence(level, action, score)
                ),
                "template": "NO_DATA",
                "risk_level": level,
            }

        if template in MOTIF_TEMPLATES:
            fired = signals.get("rules_fired") or []
            top = next(
                (r for r in fired if isinstance(r, dict) and r.get("rule") == template), {}
            )
            evidence = top.get("evidence") if isinstance(top.get("evidence"), dict) else {}
            rule_account = top.get("account") or account
            body = MOTIF_TEMPLATES[template](str(rule_account), evidence)

            others = [
                r.get("rule") for r in fired
                if isinstance(r, dict) and r.get("rule") != template
            ]
            also = ""
            if others:
                also = (
                    f" The same account also matched {', '.join(sorted(set(map(str, others))))}"
                    f", which did not outweigh the pattern above."
                )
            return {
                "explanation": body + also + _detector_sentence(signals)
                + _flag_sentence(signals) + _counterparty_sentence(signals)
                + _action_sentence(level, action, score),
                "template": template,
                "risk_level": level,
            }

        if template == "PURE_ANOMALY":
            return {
                "explanation": (
                    f"No named laundering pattern matched {account}, but its transactions are "
                    f"statistical outliers against the wider population."
                    + _detector_sentence(signals) + _flag_sentence(signals)
                    + _counterparty_sentence(signals)
                    + _action_sentence(level, action, score)
                ),
                "template": "PURE_ANOMALY",
                "risk_level": level,
            }

        if template == "ROW_FLAGS_ONLY":
            return {
                "explanation": (
                    f"No laundering pattern matched {account} and its transactions are not "
                    f"statistical outliers, but individual transaction attributes are worth "
                    f"noting."
                    + _flag_sentence(signals) + _detector_sentence(signals)
                    + _counterparty_sentence(signals)
                    + _action_sentence(level, action, score)
                ),
                "template": "ROW_FLAGS_ONLY",
                "risk_level": level,
            }

        return {
            "explanation": (
                f"No laundering pattern matched {account}, and its transactions fall within "
                f"normal statistical ranges for this dataset."
                + _detector_sentence(signals) + _counterparty_sentence(signals)
                + _action_sentence(level, action, score)
            ),
            "template": "NO_CONCERN",
            "risk_level": level,
        }
    except Exception as exc:
        return {
            "explanation": (
                f"No assessment could be produced: explanation generation failed ({exc}). "
                f"This is a failure to analyse, not a finding that the activity is clean."
            ),
            "template": "ERROR",
            "error": str(exc),
        }
