"""Deterministic study scoring; generated Python executes only in a restricted container."""
from __future__ import annotations

import copy
import csv
from decimal import Decimal, InvalidOperation
import itertools
import json
import logging
from pathlib import Path
import random
import re
import resource
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).parent
FIXTURES = ROOT / "agents/model-comparison/fixtures"
LOG = logging.getLogger(__name__)


class EvaluationInfrastructureError(RuntimeError):
    pass


def result(parts: dict[str, bool], contract: bool = True, detail: str | None = None) -> dict:
    passed = sum(parts.values())
    return {"contract_ok": contract, "correct": contract and bool(parts) and passed == len(parts),
            "checks_passed": passed, "checks_total": len(parts), "checks": parts, "detail": detail}


def decimal_equal(value: object, expected: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and number == Decimal(expected)
    except (InvalidOperation, ValueError):
        return False


def transform_reference() -> dict:
    categories = {}
    excluded = []
    count = 0
    with (FIXTURES / "transform.csv").open(newline="") as stream:
        for row in csv.DictReader(stream):
            reason = None
            try:
                quantity, price = Decimal(row["quantity"]), Decimal(row["unit_price"])
                if not quantity.is_finite() or not price.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                reason = "invalid_number"
            if reason is None:
                if quantity == 0 or quantity != quantity.to_integral_value():
                    reason = "invalid_quantity"
                elif price <= 0:
                    reason = "nonpositive_price"
            if reason:
                excluded.append({"row_id": row["row_id"], "reason": reason})
                continue
            count += 1
            entry = categories.setdefault(row["category"], {"rows": 0, "net": Decimal(0)})
            entry["rows"] += 1
            entry["net"] += quantity * price
    return {"valid_rows": count, "excluded_rows": excluded, "categories": categories,
            "net": sum((v["net"] for v in categories.values()), Decimal(0))}


def grade_json(case_id: str, text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        LOG.warning("Study contract failure %s: %s", case_id, error)
        return result({}, False, "invalid_json")
    if not isinstance(value, dict):
        return result({}, False, "expected_json_object")
    if case_id == "compute":
        expected = {"prime_count": 78498, "product": 48271 * 79613,
                    "modular_power": pow(7, 77, 1000003)}
        return result({k: type(value.get(k)) is int and value[k] == v for k, v in expected.items()},
                      set(value) == set(expected))
    if case_id == "constraints":
        jobs, depots = "ABCD", "XYZW"
        costs = [[9, 2, 7, 8], [6, 4, 3, 7], [5, 8, 1, 8], [7, 6, 9, 4]]
        forbidden = {"A": "Z", "B": "X", "C": "Y", "D": "Z"}
        optimum = min(sum(costs[i][depots.index(d)] for i, d in enumerate(order))
                      for order in itertools.permutations(depots)
                      if all(d != forbidden[j] for j, d in zip(jobs, order)))
        assignment = value.get("assignment")
        shape = (set(value) == {"assignment", "total_cost"} and isinstance(assignment, dict)
                 and set(assignment) == set(jobs) and all(isinstance(v, str) for v in assignment.values()))
        valid = bool(shape and set(assignment.values()) == set(depots)
                     and all(assignment[j] != forbidden[j] for j in jobs))
        cost = sum(costs[i][depots.index(assignment[j])] for i, j in enumerate(jobs)) if valid else None
        return result({"constraints": valid, "optimal": valid and cost == optimum,
                       "reported_cost": type(value.get("total_cost")) is int and value["total_cost"] == cost},
                      shape)
    if case_id == "transform":
        expected = transform_reference()
        observed = value.get("categories")
        categories_ok = isinstance(observed, dict) and set(observed) == set(expected["categories"])
        if categories_ok:
            for name, entry in expected["categories"].items():
                actual = observed[name]
                if (not isinstance(actual, dict) or set(actual) != {"rows", "net"}
                        or type(actual["rows"]) is not int or actual["rows"] != entry["rows"]
                        or not decimal_equal(actual["net"], entry["net"])):
                    categories_ok = False
        return result({
            "included": type(value.get("valid_rows")) is int and value["valid_rows"] == expected["valid_rows"],
            "exclusions": value.get("excluded_rows") == expected["excluded_rows"],
            "categories": categories_ok, "net": decimal_equal(value.get("net"), expected["net"]),
        }, set(value) == set(expected))
    if case_id == "evidence":
        with (FIXTURES / "evidence.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        expected_refs = [{"row_id": r["row_id"], "quantity": r["quantity"],
                          "unit_price": r["unit_price"]} for r in rows]
        verdicts = {"A": "contradicted", "B": "contradicted", "C": "not_established",
                    "D": "not_established", "E": "supported"}
        observed = value.get("verdicts")
        parts = {f"claim_{k}": isinstance(observed, dict) and observed.get(k) == v
                 for k, v in verdicts.items()}
        nets = value.get("net_values")
        refs = value.get("evidence")
        refs_ok = (isinstance(refs, list) and len(refs) == 4
                   and all(isinstance(r, dict) and set(r) == {"row_id", "quantity", "unit_price"}
                           and isinstance(r["row_id"], str) for r in refs)
                   and len({r["row_id"] for r in refs}) == 4
                   and {r["row_id"]: r for r in refs} == {r["row_id"]: r for r in expected_refs})
        parts.update({
            "net_values": isinstance(nets, dict) and set(nets) == {"P1", "P2"}
                and decimal_equal(nets["P1"], 900) and decimal_equal(nets["P2"], 780),
            "exact_cells": refs_ok,
            "limitations_present": isinstance(value.get("limitations"), list)
                and bool(value["limitations"])
                and all(isinstance(x, str) and x.strip() for x in value["limitations"]),
        })
        return dict(result(parts, set(value) == {"net_values", "verdicts", "evidence", "limitations"}
                           and isinstance(observed, dict) and set(observed) == set(verdicts)),
                    semantic_limitations_review="manual_blinded_review_required")
    raise ValueError(f"Unknown JSON study case: {case_id}")


def interval_reference(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError
    pairs = []
    for item in value:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or any(type(n) is not int for n in item) or item[0] > item[1]):
            raise ValueError
        pairs.append(list(item))
    merged = []
    for start, end in sorted(pairs):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def reconcile_reference(rows):
    if not isinstance(rows, (list, tuple)):
        raise ValueError
    seen, totals = {}, {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "account", "amount_cents"}
                or not isinstance(row["id"], str) or not row["id"]
                or not isinstance(row["account"], str) or not row["account"]
                or type(row["amount_cents"]) is not int):
            raise ValueError
        pair = (row["account"], row["amount_cents"])
        if row["id"] in seen:
            if seen[row["id"]] != pair:
                raise ValueError
            continue
        seen[row["id"]] = pair
        totals[row["account"]] = totals.get(row["account"], 0) + row["amount_cents"]
    return totals


def wire(value):
    if isinstance(value, tuple):
        return {"$tuple": [wire(v) for v in value]}
    if isinstance(value, list):
        return [wire(v) for v in value]
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    return value


def code_tests(case_id: str) -> tuple[str, list[dict], list[dict]]:
    rng = random.Random(7319)
    if case_id == "implement":
        function, reference = "normalize_intervals", interval_reference
        values = [[], [[1, 2], [3, 4]], [[4, 8], [1, 4]], [[-4, -1], [-2, 2]],
                  ((1, 2), (2, 3)), [[1, 2], (3, 4)],
                  None, "bad", [[2, 1]], [[True, 2]], [[1]], [[1, 2, 3]], [None]]
        for _ in range(30):
            values.append([[a, a + rng.randint(0, 5)] for a in
                           [rng.randint(-10, 10) for _ in range(rng.randint(0, 10))]])
    elif case_id == "debug":
        function, reference = "reconcile", reconcile_reference
        one = {"id": "a", "account": "X", "amount_cents": 10}
        values = [[], [one, dict(one)], [one, dict(one, amount_cents=-3)],
                  [one, dict(one, account="Y")], [one, dict(one, id="b", amount_cents=-10)],
                  None, "bad", [{}], [dict(one, amount_cents=True)], [dict(one, id="")],
                  [dict(one, account="")], [dict(one, extra=1)], (one,),
                  [dict(one, id=" ", account=" ", amount_cents=0)]]
        for _ in range(30):
            value = [{"id": str(i), "account": rng.choice("XYZ"), "amount_cents": rng.randint(-20, 20)}
                     for i in range(rng.randint(1, 12))]
            values.append(value + [dict(value[0])])
    else:
        raise ValueError("Unknown coding case.")
    inputs, expected = [], []
    for value in values:
        inputs.append({"args": wire([value])})
        try:
            expected.append({"value": reference(copy.deepcopy(value))})
        except ValueError:
            expected.append({"error": "ValueError"})
    return function, inputs, expected


WORKER = """import copy,importlib.util,json,sys
def wire(v):
    if isinstance(v,tuple): return {'$tuple':[wire(x) for x in v]}
    if isinstance(v,list): return [wire(x) for x in v]
    if isinstance(v,dict): return {k:wire(x) for k,x in v.items()}
    return v
def decode(v):
    if isinstance(v,dict) and set(v)=={'$tuple'}: return tuple(decode(x) for x in v['$tuple'])
    if isinstance(v,list): return [decode(x) for x in v]
    if isinstance(v,dict): return {k:decode(x) for k,x in v.items()}
    return v
inputs=json.load(open('/grade/inputs.json'))
spec=importlib.util.spec_from_file_location('candidate','/grade/solution.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
function=getattr(module,sys.argv[1])
outputs=[]
for item in inputs:
    args=decode(item['args'])
    try:
        record={'value':wire(function(*args))}
    except Exception as error:
        record={'error':type(error).__name__}
    record['args_after']=wire(args)
    outputs.append(record)
print(json.dumps(outputs,allow_nan=False))
"""


def _limit_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))


def grade_code(case_id: str, candidate: Path, image: str, output: Path) -> dict:
    if not re.fullmatch(r"(?:docker.io/library/)?python@sha256:[0-9a-f]{64}", image):
        raise EvaluationInfrastructureError("Require an immutable official Python image digest.")
    if not candidate.is_file() or candidate.stat().st_size > 65536:
        return result({}, False, "missing_or_oversized_code")
    function, inputs, expected = code_tests(case_id)
    output.mkdir(parents=True, exist_ok=False)
    name = "model-grade-" + uuid4().hex[:16]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="model-grade-") as temporary:
        work = Path(temporary)
        work.chmod(0o755)
        shutil.copyfile(candidate, work / "solution.py")
        (work / "worker.py").write_text(WORKER)
        (work / "inputs.json").write_text(json.dumps(inputs))
        for path in work.iterdir():
            path.chmod(0o644)
        command = [
            "docker", "run", "--rm", "--pull=never", "--name", name, "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=64", "--memory=256m", "--cpus=1", "--user=65534:65534",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            "--mount", f"type=bind,src={work.resolve()},dst=/grade,readonly",
            "--entrypoint=/usr/local/bin/python", image, "-I", "-B", "/grade/worker.py", function,
        ]
        try:
            with (output / "container.log").open("wb") as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                         timeout=30, preexec_fn=_limit_output)
        except subprocess.TimeoutExpired:
            return dict(result({}, False, "candidate_execution_timeout"), evaluation_seconds=time.monotonic()-started)
        except OSError as error:
            raise EvaluationInfrastructureError(f"Docker evaluation failed: {error}") from error
        finally:
            cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                raise EvaluationInfrastructureError("Could not confirm cleanup of the owned grading container.")
        text = (output / "container.log").read_text(errors="replace")
        if process.returncode == 125:
            raise EvaluationInfrastructureError("Docker rejected sandbox startup; inspect container.log.")
        if process.returncode:
            return dict(result({}, False, "candidate_execution_failed"), evaluation_seconds=time.monotonic()-started)
        try:
            observed = json.loads(text)
        except json.JSONDecodeError:
            return result({}, False, "candidate_stdout_not_json")
        if not isinstance(observed, list) or len(observed) != len(expected):
            return result({}, False, "candidate_output_shape")
        checks = {}
        for i, (actual, wanted, item) in enumerate(zip(observed, expected, inputs)):
            checks[f"test_{i}"] = (isinstance(actual, dict) and actual.get("args_after") == item["args"]
                                   and {k: v for k, v in actual.items() if k != "args_after"} == wanted)
        return dict(result(checks), evaluation_seconds=time.monotonic()-started,
                    isolation="network-none/read-only/unprivileged/capped/Python-digest-pinned")
