"""Deterministic historical transaction-value references and exact-cell report checks."""
from __future__ import annotations

from collections import defaultdict
import csv
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path

from harness_client import HarnessError


def amount_fields(quantity: str, price: str) -> tuple[Decimal | None, Decimal | None, str]:
    reasons = []
    values = []
    for name, raw in [("quantity", quantity), ("unit_price", price)]:
        try:
            value = Decimal(raw)
            if not value.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            reasons.append("invalid_" + name)
            values.append(None)
            continue
        values.append(value)
        if name == "quantity" and (value == 0 or value != value.to_integral_value()):
            reasons.append("zero_or_noninteger_quantity")
        elif name == "unit_price" and value <= 0:
            reasons.append("nonpositive_unit_price")
    return values[0], values[1], ";".join(reasons)


def metrics(rows: list[dict]) -> dict:
    counts = {
        "rows_total": len(rows), "included_rows": 0, "excluded_rows": 0,
        "cancellation_rows": 0, "included_cancellation_rows": 0,
        "negative_non_cancellation_rows": 0, "positive_cancellation_rows": 0,
        "missing_customer_rows": 0,
    }
    with localcontext() as context:
        context.prec = 60
        positive = negative = cancellation_value = Decimal(0)
        for row in rows:
            quantity, price, reason = amount_fields(row["quantity"], row["unit_price"])
            cancelled = row["invoice"].lower().startswith("c")
            if (row["included"] != str(not reason).lower()
                    or row["exclusion_reason"] != reason
                    or row["cancellation_flag"] != str(cancelled).lower()):
                raise HarnessError("Prepared inclusion/cancellation flags disagree with raw values.")
            counts["cancellation_rows"] += int(cancelled)
            counts["missing_customer_rows"] += int(row["customer_id"] == "")
            if reason:
                counts["excluded_rows"] += 1
                continue
            if quantity is None or price is None:
                raise HarnessError("Included row has missing arithmetic inputs.")
            counts["included_rows"] += 1
            value = quantity * price
            if quantity > 0:
                positive += value
            else:
                negative -= value
            counts["included_cancellation_rows"] += int(cancelled)
            counts["positive_cancellation_rows"] += int(cancelled and quantity > 0)
            counts["negative_non_cancellation_rows"] += int(not cancelled and quantity < 0)
            if cancelled:
                cancellation_value += value
        return dict(counts, gross_positive_value=str(positive),
                    negative_value_magnitude=str(negative),
                    net_transaction_value=str(positive - negative),
                    cancellation_signed_value=str(cancellation_value))


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    seen = set()
    for row in rows:
        if None in row or None in row.values() or not row.get("source_row_id"):
            raise HarnessError("Malformed evidence CSV record.")
        if row["source_row_id"] in seen:
            raise HarnessError("Duplicate source record ID; duplicate transaction values are permitted.")
        seen.add(row["source_row_id"])
    return rows


def references(path: Path) -> dict:
    rows = read_rows(path)
    periods = defaultdict(list)
    for row in rows:
        periods[row["period"]].append(row)
    return {"periods": {name: metrics(values) for name, values in sorted(periods.items())},
            "cumulative": metrics(rows)}


def check_report(report: dict, evidence: Path, stage: dict, previous: dict | None = None) -> dict:
    import hashlib
    if (type(report.get("schema_version")) is not int or report["schema_version"] != 1
            or report.get("stage_id") != stage["id"]
            or report.get("evidence_sha256") != hashlib.sha256(evidence.read_bytes()).hexdigest()
            or report.get("measure") != "derived_net_transaction_value_not_profit"):
        raise HarnessError("Retail report identity/measure mismatch.")
    expected = references(evidence)
    actual = report.get("metrics")
    if not isinstance(actual, dict) or actual.keys() != expected.keys():
        raise HarnessError("Retail report metrics shape mismatch.")
    groups = [(actual.get("cumulative"), expected["cumulative"])]
    if not isinstance(actual.get("periods"), dict) or actual["periods"].keys() != expected["periods"].keys():
        raise HarnessError("Retail report period coverage mismatch.")
    groups.extend((actual["periods"][p], v) for p, v in expected["periods"].items())
    for observed, wanted in groups:
        if not isinstance(observed, dict) or observed.keys() != wanted.keys():
            raise HarnessError("Retail metric fields mismatch.")
        for key, value in wanted.items():
            if type(value) is int:
                if type(observed[key]) is not int or observed[key] != value:
                    raise HarnessError(f"Retail count mismatch: {key}")
            else:
                try:
                    parsed = Decimal(observed[key]) if isinstance(observed[key], str) else None
                    if parsed is None or not parsed.is_finite() or parsed != Decimal(value):
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    raise HarnessError(f"Retail decimal mismatch: {key}") from None
    by_id = {row["source_row_id"]: row for row in read_rows(evidence)}
    claims = report.get("claims")
    if not isinstance(claims, list) or not claims:
        raise HarnessError("At least one qualified retail claim is required.")
    claim_ids = set()
    for claim in claims:
        if (not isinstance(claim, dict) or not isinstance(claim.get("id"), str)
                or not claim["id"].strip() or claim["id"] in claim_ids
                or not isinstance(claim.get("kind"), str)
                or claim["kind"] not in {"observation", "inference", "unknown"}
                or not isinstance(claim.get("text"), str) or not claim["text"].strip()):
            raise HarnessError("Invalid/duplicate retail claim.")
        claim_ids.add(claim["id"])
    def evidence_refs(items: object) -> None:
        if not isinstance(items, list) or not items:
            raise HarnessError("Each claim/update requires exact evidence references.")
        for item in items:
            if not isinstance(item, dict):
                raise HarnessError("Malformed retail reference.")
            identifier = item.get("source_row_id")
            row = by_id.get(identifier) if isinstance(identifier, str) else None
            values = item.get("observed_values")
            if (row is None or not isinstance(values, dict) or not values
                    or "source_row_id" in values
                    or any(not isinstance(v, str) or k not in row or row[k] != v
                           for k, v in values.items())):
                raise HarnessError("Retail evidence reference is missing or changed.")
    for claim in claims:
        evidence_refs(claim.get("evidence_refs"))
    updates = report.get("claim_updates")
    if not isinstance(updates, list):
        raise HarnessError("claim_updates must be an explicit list.")
    wanted = {c["id"] for c in previous["claims"]} if previous else set()
    updated = set()
    for update in updates:
        if (not isinstance(update, dict) or not isinstance(update.get("prior_claim_id"), str)
                or update["prior_claim_id"] not in wanted
                or update["prior_claim_id"] in updated
                or not isinstance(update.get("disposition"), str)
                or update["disposition"] not in {"retain", "revise", "withdraw"}
                or not isinstance(update.get("reason"), str) or not update["reason"].strip()):
            raise HarnessError("Invalid retail claim-update disposition.")
        updated.add(update["prior_claim_id"])
        evidence_refs(update.get("evidence_refs"))
    if updated != wanted:
        raise HarnessError("Every prior claim must have exactly one disposition.")
    if (not isinstance(report.get("limitations"), list) or not report["limitations"]
            or any(not isinstance(x, str) or not x.strip() for x in report["limitations"])):
        raise HarnessError("Retail limitations required.")
    return {"status": "mechanically_valid", "claims": len(claims),
            "semantic_grounding_and_revision_quality": "manual_review_required",
            "scope": "Exact arithmetic, current-file citations and prior-claim accounting only."}
