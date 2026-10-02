"""paymaster.sponsorship.evaluate_sponsorship 的单元测试。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paymaster.sponsorship import (  # noqa: E402
    E_BUDGET,
    E_GAS_LIMIT,
    E_POLICY_INVALID_FIELD,
    E_POLICY_MISSING_FIELD,
    E_POLICY_UNKNOWN_FIELD,
    OK,
    evaluate_sponsorship,
)
from paymaster.validation import E_MISSING_FIELD  # noqa: E402

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
PAYMASTER = "0x3333333333333333333333333333333333333333"

# 最小请求：totalGas = 0x10000 + 0x20000 + 0x30000 = 0x60000 = 393216；
# maxFeePerGas = 0x10 = 16，estimatedCostWei = 6291456。
TOTAL_GAS = 0x60000
ESTIMATED_COST = TOTAL_GAS * 0x10


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


def valid_policy(budget: int = ESTIMATED_COST, max_gas: int = TOTAL_GAS) -> dict:
    return {"budgetWei": hex(budget), "maxTotalGas": hex(max_gas)}


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


def decision_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result["decision"]


class TestDecision(unittest.TestCase):
    def test_approved_within_limits(self):
        decision = decision_of(evaluate_sponsorship(valid_request(), valid_policy()))
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["reason"], OK)
        self.assertEqual(decision["totalGas"], str(TOTAL_GAS))
        self.assertEqual(decision["estimatedCostWei"], str(ESTIMATED_COST))

    def test_total_gas_equals_max_total_gas_approved(self):
        policy = valid_policy(max_gas=TOTAL_GAS)
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["reason"], OK)

    def test_total_gas_exceeds_max_total_gas(self):
        policy = valid_policy(max_gas=TOTAL_GAS - 1)
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertFalse(decision["approved"])
        self.assertEqual(decision["reason"], E_GAS_LIMIT)
        self.assertEqual(decision["totalGas"], str(TOTAL_GAS))
        self.assertEqual(decision["estimatedCostWei"], str(ESTIMATED_COST))

    def test_cost_equals_budget_approved(self):
        policy = valid_policy(budget=ESTIMATED_COST)
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["reason"], OK)

    def test_cost_exceeds_budget(self):
        policy = valid_policy(budget=ESTIMATED_COST - 1)
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertFalse(decision["approved"])
        self.assertEqual(decision["reason"], E_BUDGET)

    def test_gas_limit_takes_precedence_over_budget(self):
        policy = valid_policy(budget=1, max_gas=TOTAL_GAS - 1)
        decision = decision_of(evaluate_sponsorship(valid_request(), policy))
        self.assertFalse(decision["approved"])
        self.assertEqual(decision["reason"], E_GAS_LIMIT)

    def test_optional_paymaster_gas_fields_included(self):
        req = valid_request()
        req["userOperation"].update(
            {
                "paymaster": PAYMASTER,
                "paymasterVerificationGasLimit": "0x40000",
                "paymasterPostOpGasLimit": "0x50000",
                "paymasterData": "0x",
            }
        )
        total = TOTAL_GAS + 0x40000 + 0x50000
        decision = decision_of(
            evaluate_sponsorship(req, valid_policy(max_gas=total, budget=total * 0x10))
        )
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["totalGas"], str(total))
        self.assertEqual(decision["estimatedCostWei"], str(total * 0x10))

    def test_zero_max_fee_gives_zero_cost(self):
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0x0"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x0"
        decision = decision_of(
            evaluate_sponsorship(req, {"budgetWei": "0x1", "maxTotalGas": hex(TOTAL_GAS)})
        )
        self.assertTrue(decision["approved"])
        self.assertEqual(decision["estimatedCostWei"], "0")


class TestRequestValidation(unittest.TestCase):
    def test_invalid_request_returns_existing_error_unchanged(self):
        req = valid_request()
        del req["userOperation"]["signature"]
        err = error_of(evaluate_sponsorship(req, valid_policy()))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/userOperation/signature")

    def test_invalid_request_skips_policy_judgement(self):
        # 即便 policy 本身也非法，request 错误原样先返回。
        err = error_of(evaluate_sponsorship("not-an-object", []))
        self.assertEqual(err["code"], "E_INVALID_JSON")
        self.assertEqual(err["path"], "")

    def test_request_combination_error_propagated(self):
        req = valid_request()
        req["userOperation"]["factoryData"] = "0x"
        err = error_of(evaluate_sponsorship(req, valid_policy()))
        self.assertEqual(err["code"], "E_FIELD_COMBINATION")


class TestPolicyValidation(unittest.TestCase):
    def _policy_error(self, policy):
        return error_of(evaluate_sponsorship(valid_request(), policy))

    def test_policy_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(bad=bad):
                err = self._policy_error(bad)
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], "")

    def test_missing_budget(self):
        policy = {"maxTotalGas": hex(TOTAL_GAS)}
        err = self._policy_error(policy)
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")
        self.assertIn("budgetWei", err["message"])

    def test_missing_max_total_gas(self):
        policy = {"budgetWei": hex(ESTIMATED_COST)}
        err = self._policy_error(policy)
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")
        self.assertIn("maxTotalGas", err["message"])

    def test_missing_field_takes_precedence_over_unknown_field(self):
        policy = {"budgetWei": hex(ESTIMATED_COST), "extra": "0x1"}
        err = self._policy_error(policy)
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")

    def test_unknown_field(self):
        policy = valid_policy()
        policy["extra"] = "0x1"
        err = self._policy_error(policy)
        self.assertEqual(err["code"], E_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")
        self.assertIn("extra", err["message"])

    def test_empty_object_reports_missing_budget_first(self):
        err = self._policy_error({})
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_budget_checked_before_max_total_gas(self):
        policy = {"budgetWei": "bad", "maxTotalGas": "also-bad"}
        err = self._policy_error(policy)
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_invalid_quantity_values(self):
        for field in ("budgetWei", "maxTotalGas"):
            for bad in (7, True, None, "0x", "0x01", "10", "0xZZ"):
                with self.subTest(field=field, bad=bad):
                    policy = valid_policy()
                    policy[field] = bad
                    err = self._policy_error(policy)
                    self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                    self.assertEqual(err["path"], f"/{field}")

    def test_zero_values_invalid(self):
        for field in ("budgetWei", "maxTotalGas"):
            with self.subTest(field=field):
                policy = valid_policy()
                policy[field] = "0x0"
                err = self._policy_error(policy)
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], f"/{field}")
                self.assertIn("greater than 0", err["message"])

    def test_quantity_over_32_bytes_invalid(self):
        policy = valid_policy()
        policy["budgetWei"] = "0x" + "1" + "0" * 64
        err = self._policy_error(policy)
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_booleans_are_not_quantities(self):
        # bool 是 int 子类，必须仍被当作非法值。
        err = self._policy_error(
            {"budgetWei": True, "maxTotalGas": True}
        )
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/budgetWei")


class TestNoMutation(unittest.TestCase):
    def test_inputs_not_modified(self):
        req = valid_request()
        policy = valid_policy()
        req_snapshot = copy.deepcopy(req)
        policy_snapshot = copy.deepcopy(policy)
        evaluate_sponsorship(req, policy)
        self.assertEqual(req, req_snapshot)
        self.assertEqual(policy, policy_snapshot)


if __name__ == "__main__":
    unittest.main()
