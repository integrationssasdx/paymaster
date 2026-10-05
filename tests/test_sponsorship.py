"""paymaster.sponsorship 与其 CLI 的单元测试。运行：python -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paymaster.sponsorship import (  # noqa: E402
    E_POLICY_INVALID_FIELD,
    E_POLICY_MISSING_FIELD,
    E_POLICY_UNKNOWN_FIELD,
    evaluate_sponsorship,
)
from paymaster.validation import E_INVALID_JSON, E_MISSING_FIELD  # noqa: E402

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
PAYMASTER = "0x3333333333333333333333333333333333333333"


def valid_request() -> dict:
    return {
        "userOperation": {
            "sender": SENDER,
            "nonce": "0x0",
            "callData": "0x",
            "callGasLimit": "0x10000",
            "verificationGasLimit": "0x20000",
            "preVerificationGas": "0x30000",
            "maxFeePerGas": "0x10",
            "maxPriorityFeePerGas": "0x10",
            "signature": "0x",
        },
        "context": {
            "version": "0.7",
            "chainId": "0x1",
            "entryPoint": ENTRY_POINT,
            "baseFeePerGas": "0x7",
        },
    }


def valid_policy() -> dict:
    return {"budgetWei": "0xde0b6b3a7640000", "maxTotalGas": "0x100000"}


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


def decision_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result["decision"]


class TestDecision(unittest.TestCase):
    def test_approved_ok(self):
        # totalGas = 0x10000 + 0x20000 + 0x30000 = 0x60000 = 393216
        # effectiveGasPriceWei = min(0x10, 0x7 + 0x10) = 0x10
        # cost = 393216 * 16 = 6291456
        decision = decision_of(evaluate_sponsorship(valid_request(), valid_policy()))
        self.assertEqual(decision["approved"], True)
        self.assertEqual(decision["reason"], "OK")
        self.assertEqual(decision["totalGas"], "393216")
        self.assertEqual(decision["effectiveGasPriceWei"], "16")
        self.assertEqual(decision["estimatedCostWei"], "6291456")

    def test_effective_gas_price_uses_base_fee_plus_tip_when_below_cap(self):
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0x100"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x0"
        # min(0x100, 0x7 + 0x0) = 0x7
        decision = decision_of(evaluate_sponsorship(req, valid_policy()))
        self.assertEqual(decision["effectiveGasPriceWei"], "7")
        self.assertEqual(decision["estimatedCostWei"], str(393216 * 7))

    def test_effective_gas_price_when_base_plus_tip_equals_cap(self):
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0xa"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x5"
        req["context"]["baseFeePerGas"] = "0x5"
        # min(0xa, 0x5 + 0x5) = 0xa
        decision = decision_of(evaluate_sponsorship(req, valid_policy()))
        self.assertEqual(decision["effectiveGasPriceWei"], "10")
        self.assertEqual(decision["estimatedCostWei"], str(393216 * 10))

    def test_cost_based_on_effective_price_not_max_fee(self):
        # cap 很大但 tip 为 0：旧公式按 cap 计必超预算，新公式按 baseFee 计通过。
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0x100"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x0"
        policy = valid_policy()
        policy["budgetWei"] = hex(393216 * 8)  # 在 393216*7 与 393216*256 之间
        decision = decision_of(evaluate_sponsorship(req, policy))
        self.assertEqual(decision["approved"], True)
        self.assertEqual(decision["reason"], "OK")
        self.assertEqual(decision["estimatedCostWei"], str(393216 * 7))

    def test_cost_above_budget_uses_effective_price(self):
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0x100"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x0"
        policy = valid_policy()
        policy["budgetWei"] = hex(393216 * 7 - 1)
        decision = decision_of(evaluate_sponsorship(req, policy))
        self.assertEqual(decision["approved"], False)
        self.assertEqual(decision["reason"], "E_BUDGET")

    def test_cost_equal_budget_uses_effective_price(self):
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0x100"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x0"
        policy = valid_policy()
        policy["budgetWei"] = hex(393216 * 7)
        decision = decision_of(evaluate_sponsorship(req, policy))
        self.assertEqual(decision["approved"], True)
        self.assertEqual(decision["reason"], "OK")

    def test_paymaster_gas_fields_included_in_total(self):
        req = valid_request()
        req["userOperation"].update(
            paymaster=PAYMASTER,
            paymasterVerificationGasLimit="0x40000",
            paymasterPostOpGasLimit="0x50000",
            paymasterData="0x",
        )
        # totalGas = 0x60000 + 0x40000 + 0x50000 = 0xf0000 = 983040
        decision = decision_of(evaluate_sponsorship(req, valid_policy()))
        self.assertEqual(decision["totalGas"], "983040")
        self.assertEqual(decision["effectiveGasPriceWei"], "16")
        self.assertEqual(decision["estimatedCostWei"], str(983040 * 16))

    def test_total_gas_above_limit_rejected(self):
        policy = valid_policy()
        policy["maxTotalGas"] = "0x5ffff"  # 393215 < 393216
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(decision["approved"], False)
        self.assertEqual(decision["reason"], "E_GAS_LIMIT")

    def test_total_gas_equal_limit_approved(self):
        policy = valid_policy()
        policy["maxTotalGas"] = "0x60000"
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(decision["approved"], True)
        self.assertEqual(decision["reason"], "OK")

    def test_cost_above_budget_rejected(self):
        policy = valid_policy()
        policy["budgetWei"] = "0x5fffff"  # 6291455 < 6291456
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(decision["approved"], False)
        self.assertEqual(decision["reason"], "E_BUDGET")

    def test_cost_equal_budget_approved(self):
        policy = valid_policy()
        policy["budgetWei"] = hex(393216 * 16)
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(decision["approved"], True)

    def test_gas_limit_checked_before_budget(self):
        policy = {"budgetWei": "0x1", "maxTotalGas": "0x1"}
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(decision["approved"], False)
        self.assertEqual(decision["reason"], "E_GAS_LIMIT")

    def test_numeric_results_are_decimal_strings(self):
        decision = decision_of(evaluate_sponsorship(valid_request(), valid_policy()))
        self.assertEqual(
            set(decision.keys()),
            {
                "approved",
                "reason",
                "totalGas",
                "estimatedCostWei",
                "effectiveGasPriceWei",
            },
        )
        for key in ("totalGas", "estimatedCostWei", "effectiveGasPriceWei"):
            self.assertIsInstance(decision[key], str)
            self.assertRegex(decision[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        req = valid_request()
        policy = valid_policy()
        req_copy = copy.deepcopy(req)
        policy_copy = copy.deepcopy(policy)
        evaluate_sponsorship(req, policy)
        self.assertEqual(req, req_copy)
        self.assertEqual(policy, policy_copy)


class TestRequestErrorsPassthrough(unittest.TestCase):
    def test_invalid_request_returns_existing_error(self):
        req = valid_request()
        del req["userOperation"]["signature"]
        result = evaluate_sponsorship(req, valid_policy())
        err = error_of(result)
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/userOperation/signature")
        self.assertNotIn("decision", result)

    def test_request_root_not_object(self):
        err = error_of(evaluate_sponsorship([], valid_policy()))
        self.assertEqual(err["code"], E_INVALID_JSON)
        self.assertEqual(err["path"], "")

    def test_request_checked_before_policy(self):
        req = valid_request()
        del req["userOperation"]["sender"]
        err = error_of(evaluate_sponsorship(req, "not a policy"))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/userOperation/sender")


class TestPolicyValidation(unittest.TestCase):
    def test_policy_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(policy=bad):
                err = error_of(evaluate_sponsorship(valid_request(), bad))
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], "")

    def test_missing_key(self):
        err = error_of(evaluate_sponsorship(valid_request(), {"maxTotalGas": "0x1"}))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")
        self.assertIn("budgetWei", err["message"])

    def test_missing_key_order(self):
        err = error_of(evaluate_sponsorship(valid_request(), {}))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_missing_checked_before_unknown(self):
        policy = {"maxTotalGas": "0x1", "extra": "0x1"}
        err = error_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_unknown_key(self):
        policy = valid_policy()
        policy["maxGas"] = "0x1"
        err = error_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(err["code"], E_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/maxGas")
        self.assertIn("maxGas", err["message"])

    def test_value_not_string(self):
        policy = valid_policy()
        policy["budgetWei"] = 100
        err = error_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_value_not_canonical_quantity(self):
        for bad in ("0x", "0x01", "100", "0x" + "1" * 65):
            with self.subTest(value=bad):
                policy = valid_policy()
                policy["maxTotalGas"] = bad
                err = error_of(evaluate_sponsorship(valid_request(), policy))
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], "/maxTotalGas")

    def test_value_zero_rejected(self):
        for field in ("budgetWei", "maxTotalGas"):
            with self.subTest(field=field):
                policy = valid_policy()
                policy[field] = "0x0"
                err = error_of(evaluate_sponsorship(valid_request(), policy))
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], f"/{field}")

    def test_budget_checked_before_max_total_gas(self):
        policy = {"budgetWei": "0x0", "maxTotalGas": "nope"}
        err = error_of(evaluate_sponsorship(valid_request(), policy))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/budgetWei")


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.sponsor"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout

    def test_approved_exit_0_single_json_doc(self):
        doc = {"request": valid_request(), "policy": valid_policy()}
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        docs = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(docs), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertTrue(result["decision"]["approved"])

    def test_rejected_decision_still_exit_0(self):
        doc = {"request": valid_request(), "policy": {"budgetWei": "0x1", "maxTotalGas": "0x1"}}
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertFalse(result["decision"]["approved"])

    def test_invalid_request_exit_1(self):
        req = valid_request()
        del req["userOperation"]["sender"]
        code, out = self.run_cli(json.dumps({"request": req, "policy": valid_policy()}))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_MISSING_FIELD)

    def test_invalid_policy_exit_1(self):
        code, out = self.run_cli(json.dumps({"request": valid_request(), "policy": {}}))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_POLICY_MISSING_FIELD)

    def test_malformed_json_exit_1(self):
        code, out = self.run_cli("{not json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)

    def test_non_object_document_exit_1(self):
        code, out = self.run_cli("[1, 2]")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)


if __name__ == "__main__":
    unittest.main()
