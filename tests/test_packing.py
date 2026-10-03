"""paymaster.packing 与其 CLI 的单元测试。运行：python -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paymaster.packing import plan_bundle  # noqa: E402
from paymaster.sponsorship import (  # noqa: E402
    E_POLICY_INVALID_FIELD,
    E_POLICY_MISSING_FIELD,
    E_POLICY_UNKNOWN_FIELD,
)
from paymaster.validation import (  # noqa: E402
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
)

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# 单笔请求的 totalGas = 0x10000 + 0x20000 + 0x30000 = 0x60000 = 393216
ITEM_GAS = 393216
# 单笔请求的 estimatedCostWei = 393216 * 0x10 = 6291456
ITEM_COST = 6291456


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


def valid_document(count: int = 2) -> dict:
    return {
        "requests": [valid_request() for _ in range(count)],
        "sponsorshipPolicy": {"budgetWei": "0xde0b6b3a7640000", "maxTotalGas": "0x100000"},
        "bundlePolicy": {"maxTotalGas": "0x1000000", "maxCostWei": "0xde0b6b3a7640000"},
    }


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


def plan_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result["plan"]


class TestPlan(unittest.TestCase):
    def test_all_selected(self):
        plan = plan_of(plan_bundle(valid_document(2)))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))

    def test_empty_requests_succeeds(self):
        plan = plan_of(plan_bundle(valid_document(0)))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_empty_selection_succeeds(self):
        doc = valid_document(1)
        doc["sponsorshipPolicy"]["maxTotalGas"] = "0x1"  # 单笔即被拒
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["operationCount"], "0")

    def test_unapproved_request_skipped_with_reason(self):
        doc = valid_document(2)
        doc["requests"][1]["userOperation"]["preVerificationGas"] = "0x200000"
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["totalGas"], str(ITEM_GAS))

    def test_unapproved_budget_reason_kept(self):
        doc = valid_document(1)
        doc["requests"][0]["userOperation"]["maxFeePerGas"] = "0xde0b6b3a7640000"
        doc["requests"][0]["userOperation"]["maxPriorityFeePerGas"] = "0x10"
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUDGET"}])

    def test_bundle_gas_limit(self):
        doc = valid_document(2)
        doc["bundlePolicy"]["maxTotalGas"] = hex(ITEM_GAS)  # 仅容下一笔
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])
        self.assertEqual(plan["totalGas"], str(ITEM_GAS))

    def test_bundle_budget(self):
        doc = valid_document(2)
        doc["bundlePolicy"]["maxCostWei"] = hex(ITEM_COST)  # 仅容下一笔
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_BUDGET"}])
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST))

    def test_bundle_gas_checked_before_budget(self):
        doc = valid_document(2)
        doc["bundlePolicy"]["maxTotalGas"] = hex(ITEM_GAS)
        doc["bundlePolicy"]["maxCostWei"] = hex(ITEM_COST)
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_bundle_limits_are_cumulative(self):
        doc = valid_document(3)
        doc["bundlePolicy"]["maxTotalGas"] = hex(ITEM_GAS * 2)
        plan = plan_of(plan_bundle(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_BUNDLE_GAS"}])

    def test_summary_values_are_decimal_strings(self):
        plan = plan_of(plan_bundle(valid_document(2)))
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        doc = valid_document(2)
        doc_copy = copy.deepcopy(doc)
        plan_bundle(doc)
        self.assertEqual(doc, doc_copy)


class TestDocumentStructure(unittest.TestCase):
    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = valid_document(1)
        doc["extra"] = {}
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")

    def test_unknown_checked_before_missing(self):
        err = error_of(plan_bundle({"requests": [], "extra": 1}))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")

    def test_missing_key_order(self):
        err = error_of(plan_bundle({}))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests")
        err = error_of(plan_bundle({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle({"requests": [], "sponsorshipPolicy": {}})
        )
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = valid_document(1)
                doc["requests"] = bad
                err = error_of(plan_bundle(doc))
                self.assertEqual(err["code"], E_INVALID_FIELD)
                self.assertEqual(err["path"], "/requests")


class TestRequestErrors(unittest.TestCase):
    def test_request_error_path_prefixed_with_index(self):
        doc = valid_document(2)
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests/1/userOperation/signature")

    def test_request_root_error_path(self):
        doc = valid_document(1)
        doc["requests"][0] = []
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_INVALID_JSON)
        self.assertEqual(err["path"], "/requests/0")

    def test_first_invalid_request_reported(self):
        doc = valid_document(2)
        del doc["requests"][0]["userOperation"]["sender"]
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["path"], "/requests/0/userOperation/sender")

    def test_requests_checked_before_policies(self):
        doc = valid_document(1)
        del doc["requests"][0]["userOperation"]["sender"]
        doc["sponsorshipPolicy"] = "not a policy"
        doc["bundlePolicy"] = "not a policy"
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests/0/userOperation/sender")


class TestPolicyErrors(unittest.TestCase):
    def test_sponsorship_policy_not_object(self):
        doc = valid_document(1)
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/sponsorshipPolicy")

    def test_sponsorship_policy_missing_key(self):
        doc = valid_document(1)
        del doc["sponsorshipPolicy"]["budgetWei"]
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/sponsorshipPolicy/budgetWei")

    def test_sponsorship_policy_unknown_key(self):
        doc = valid_document(1)
        doc["sponsorshipPolicy"]["extra"] = "0x1"
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/sponsorshipPolicy/extra")

    def test_sponsorship_policy_invalid_value(self):
        doc = valid_document(1)
        doc["sponsorshipPolicy"]["maxTotalGas"] = "0x0"
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/sponsorshipPolicy/maxTotalGas")

    def test_bundle_policy_not_object(self):
        doc = valid_document(1)
        doc["bundlePolicy"] = "x"
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_bundle_policy_missing_key_order(self):
        doc = valid_document(1)
        doc["bundlePolicy"] = {}
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/bundlePolicy/maxTotalGas")

    def test_bundle_policy_missing_before_unknown(self):
        doc = valid_document(1)
        doc["bundlePolicy"] = {"maxCostWei": "0x1", "extra": "0x1"}
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/bundlePolicy/maxTotalGas")

    def test_bundle_policy_unknown_key(self):
        doc = valid_document(1)
        doc["bundlePolicy"]["budgetWei"] = "0x1"
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/bundlePolicy/budgetWei")

    def test_bundle_policy_value_order(self):
        doc = valid_document(1)
        doc["bundlePolicy"] = {"maxTotalGas": "0x0", "maxCostWei": "nope"}
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/bundlePolicy/maxTotalGas")

    def test_bundle_policy_zero_rejected(self):
        for field in ("maxTotalGas", "maxCostWei"):
            with self.subTest(field=field):
                doc = valid_document(1)
                doc["bundlePolicy"][field] = "0x0"
                err = error_of(plan_bundle(doc))
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], f"/bundlePolicy/{field}")

    def test_sponsorship_policy_checked_before_bundle_policy(self):
        doc = valid_document(1)
        doc["sponsorshipPolicy"] = {}
        doc["bundlePolicy"] = "x"
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/sponsorshipPolicy/budgetWei")


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout

    def test_success_exit_0_single_json_doc(self):
        code, out = self.run_cli(json.dumps(valid_document(2)))
        self.assertEqual(code, 0)
        docs = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(docs), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1])

    def test_empty_selection_still_exit_0(self):
        doc = valid_document(1)
        doc["sponsorshipPolicy"]["maxTotalGas"] = "0x1"
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])

    def test_structural_failure_exit_1(self):
        doc = valid_document(1)
        del doc["requests"][0]["userOperation"]["sender"]
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_MISSING_FIELD)

    def test_policy_failure_exit_1(self):
        doc = valid_document(1)
        doc["bundlePolicy"] = {}
        code, out = self.run_cli(json.dumps(doc))
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
