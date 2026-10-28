"""Calculate explicitly illustrative execution economics, never company actuals."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
from typing import Any


def calculate(assumptions: dict[str, Any]) -> dict[str, Any]:
    if assumptions.get("label") != "Illustrative assumptions only; not company actuals or forecasts":
        raise ValueError("Assumptions must carry the illustrative-only label.")
    if assumptions.get("currency") != "USD":
        raise ValueError("This sample defines USD-denominated illustrative inputs.")
    scenarios = assumptions.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("At least one scenario is required.")
    results = []
    names: set[str] = set()
    fields = {
        "monthly_attempts", "success_rate", "price_per_success",
        "cost_per_attempt", "fixed_cost_monthly",
    }
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != fields | {"name"}:
            raise ValueError("Each scenario must contain exactly the documented inputs.")
        name = scenario["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Scenario names must be nonempty and unique.")
        names.add(name)
        values: dict[str, Decimal] = {}
        for field in fields:
            value = scenario[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field} must be numeric.")
            number = Decimal(str(value))
            if not number.is_finite() or number < 0:
                raise ValueError(f"{field} must be finite and nonnegative.")
            values[field] = number
        attempts = values["monthly_attempts"]
        rate = values["success_rate"]
        price = values["price_per_success"]
        if attempts <= 0 or attempts != attempts.to_integral_value() or rate > 1 or price <= 0:
            raise ValueError("Attempts must be a positive integer, success rate <= 1, and price > 0.")
        successes = attempts * rate
        revenue = successes * price
        variable = attempts * values["cost_per_attempt"]
        gross = revenue - variable
        contribution = gross - values["fixed_cost_monthly"]

        def rounded(value: Decimal) -> float:
            return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        results.append({
            "name": name,
            "successful_executions": rounded(successes),
            "revenue": rounded(revenue),
            "variable_cost": rounded(variable),
            "gross_profit": rounded(gross),
            "gross_margin_percent": rounded(gross / revenue * 100) if revenue else None,
            "contribution_after_fixed": rounded(contribution),
            "break_even_success_rate": float(
                ((variable + values["fixed_cost_monthly"]) / (attempts * price))
                .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            ),
        })
    return {
        "label": assumptions["label"], "currency": "USD",
        "assumptions": assumptions["scenarios"], "scenarios": results,
    }


if __name__ == "__main__":
    source = Path(__file__).with_name("assumptions.json")
    print(json.dumps(calculate(json.loads(source.read_text(encoding="utf-8"))), indent=2))
