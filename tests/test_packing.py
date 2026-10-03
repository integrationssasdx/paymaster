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

from paymaster.packing import (  # noqa: E402
    E_BUNDLE_BUDGET,
    E_BUNDLE_GAS,
    plan_bundle,
)
from paymaster.validation import (  # noqa: E402
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
)
from paymaster.sponsorship import (  # noqa: E402
    E_POLICY_INVALID_FIELD,
    E_POLICY_MISSING_FIELD,
    E_POLICY_UNKNOWN_FIELD,
)

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"


def request_with_gas(call: int, verif: int, pre: int, fee: int = 16) -> dict:
    """构造一个 totalGas = call+verif+pre 的合法请求。"""
    return {
        "userOperation": {
            "sender": SENDER,
            "nonce": "0x0",
            "callData": "0x",
            "callGasLimit": hex(call),
            "verificationGasLimit": hex(verif),
            "preVerificationGas": hex(pre),
            "maxFeePerGas": hex(fee),
            "maxPriorityFeePerGas": hex(fee),
            "signature": "0x",
        },
        "context": {
            "version": "0.7",
            "chainId": "0x1",
            "entryPoint": ENTRY_POINT,
            "baseFeePerGas": "0x7",
        },
    }


def valid_request() -> dict:
    # totalGas = 0x10000 + 0x20000 + 0x30000 = 393216，cost = 393216 * 16。
    return request_with_gas(0x10000, 0x20000, 0x30000)


def sponsorship_policy(budget_wei=10**30, max_total_gas=10**12) -> dict:
    return {"budgetWei": hex(budget_wei), "maxTotalGas": hex(max_total_gas)}


def bundle_policy(max_total_gas=10**12, max_cost_wei=10**30) -> dict:
    return {"maxTotalGas": hex(max_total_gas), "maxCostWei": hex(max_cost_wei)}


def document(requests, sp=None, bp=None) -> dict:
    return {
        "requests": requests,
        "sponsorshipPolicy": sp if sp is not None else sponsorship_policy(),
        "bundlePolicy": bp if bp is not None else bundle_policy(),
    }


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


def plan_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result["plan"]


class TestPlanning(unittest.TestCase):
    def test_single_selected(self):
        plan = plan_of(plan_bundle(document([valid_request()])))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "1")
        self.assertEqual(plan["totalGas"], "393216")
        self.assertEqual(plan["estimatedCostWei"], "6291456")

    def test_empty_requests_succeeds_with_empty_selection(self):
        plan = plan_of(plan_bundle(document([])))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_all_selected_within_caps(self):
        reqs = [valid_request(), valid_request()]
        plan = plan_of(plan_bundle(document(reqs, bp=bundle_policy(10**9, 10**9))))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(393216 * 2))
        self.assertEqual(plan["estimatedCostWei"], str(6291456 * 2))

    def test_gas_cap_skips_with_e_bundle_gas(self):
        # 单笔 393216，累计上限恰好 393216：第二个累计超限。
        plan = plan_of(
            plan_bundle(document([valid_request(), valid_request()],
                                 bp=bundle_policy(max_total_gas=393216)))
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": E_BUNDLE_GAS}])
        self.assertEqual(plan["totalGas"], "393216")

    def test_gas_boundary_equal_is_selected(self):
        plan = plan_of(
            plan_bundle(document([valid_request(), valid_request()],
                                 bp=bundle_policy(max_total_gas=393216 * 2)))
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])

    def test_budget_cap_skips_with_e_bundle_budget(self):
        # gas 充足，但累计成本上限只容一笔（6291456）。
        plan = plan_of(
            plan_bundle(document([valid_request(), valid_request()],
                                 bp=bundle_policy(max_cost_wei=6291456)))
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": E_BUNDLE_BUDGET}])
        self.assertEqual(plan["estimatedCostWei"], "6291456")

    def test_cost_boundary_equal_is_selected(self):
        plan = plan_of(
            plan_bundle(document([valid_request(), valid_request()],
                                 bp=bundle_policy(max_cost_wei=6291456 * 2)))
        )
        self.assertEqual(plan["selected"], [0, 1])

    def test_gas_checked_before_cost(self):
        # 两个上限都极紧：应记 E_BUNDLE_GAS 而非 E_BUNDLE_BUDGET。
        plan = plan_of(
            plan_bundle(document([valid_request()],
                                 bp=bundle_policy(max_total_gas=1, max_cost_wei=1)))
        )
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": E_BUNDLE_GAS}])

    def test_order_is_strict_no_backfill(self):
        # A=300000, B=100000, C=100000；gas 上限 400000。
        # A+B 恰好 400000 入选，C 超限被跳过，且不会回填 B/C 替代 A。
        reqs = [
            request_with_gas(100000, 100000, 100000),
            request_with_gas(40000, 30000, 30000),
            request_with_gas(40000, 30000, 30000),
        ]
        plan = plan_of(
            plan_bundle(document(reqs, bp=bundle_policy(max_total_gas=400000)))
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": E_BUNDLE_GAS}])
        self.assertEqual(plan["totalGas"], "400000")

    def test_rejected_by_sponsorship_skipped_with_reason(self):
        # 中间一笔 totalGas=900000 超过 sponsorship 上限 500000：代付拒绝，
        # 打包跳过并沿用单笔 reason E_GAS_LIMIT，其余两笔照常入选。
        small = request_with_gas(40000, 30000, 30000)  # 100000
        big = request_with_gas(700000, 100000, 100000)  # 900000
        sp = sponsorship_policy(max_total_gas=500000)
        plan = plan_of(plan_bundle(document([small, big, small], sp=sp)))
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["totalGas"], "200000")
        self.assertEqual(plan["operationCount"], "2")

    def test_sponsorship_budget_reason_passthrough(self):
        # gas 不超限但单笔成本超 sponsorship budgetWei：reason E_BUDGET。
        req = request_with_gas(100000, 100000, 100000, fee=10**9)
        sp = sponsorship_policy(budget_wei=1, max_total_gas=10**12)
        plan = plan_of(plan_bundle(document([req], sp=sp)))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUDGET"}])

    def test_summary_values_are_decimal_strings(self):
        plan = plan_of(plan_bundle(document([valid_request()])))
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        doc = document([valid_request()])
        doc_copy = copy.deepcopy(doc)
        plan_bundle(doc)
        self.assertEqual(doc, doc_copy)


class TestRootValidation(unittest.TestCase):
    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(doc=bad):
                err = error_of(plan_bundle(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_root_key(self):
        err = error_of(plan_bundle({**document([]), "extra": 1}))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")

    def test_missing_root_key_order(self):
        # 全缺：报第一个 requests。
        err = error_of(plan_bundle({}))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests")

    def test_missing_bundle_policy(self):
        doc = document([])
        del doc["bundlePolicy"]
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_requests_not_array(self):
        err = error_of(plan_bundle(document({})))
        self.assertEqual(err["code"], E_INVALID_FIELD)
        self.assertEqual(err["path"], "/requests")

    def test_unknown_checked_before_missing(self):
        # 既有未知键，又缺 bundlePolicy：未知键优先。
        doc = document([])
        del doc["bundlePolicy"]
        doc["nope"] = 1
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/nope")


class TestRequestsValidation(unittest.TestCase):
    def test_item_error_prefixed_with_index(self):
        bad = valid_request()
        del bad["userOperation"]["signature"]
        err = error_of(plan_bundle(document([valid_request(), bad])))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests/1/userOperation/signature")
        self.assertIn("signature", err["message"])

    def test_first_bad_item_reported(self):
        bad0 = valid_request()
        del bad0["context"]["chainId"]
        err = error_of(plan_bundle(document([bad0, valid_request()])))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests/0/context/chainId")

    def test_item_root_not_object_prefix_only(self):
        # 单笔根非对象时原 path 为空，前缀恰好为 /requests/0。
        err = error_of(plan_bundle(document([[]])))
        self.assertEqual(err["code"], E_INVALID_JSON)
        self.assertEqual(err["path"], "/requests/0")

    def test_requests_checked_before_policies(self):
        bad = valid_request()
        del bad["userOperation"]["sender"]
        doc = {
            "requests": [bad],
            "sponsorshipPolicy": {},
            "bundlePolicy": [],
        }
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/requests/0/userOperation/sender")


class TestSponsorshipPolicyValidation(unittest.TestCase):
    def doc_with(self, sp):
        return {"requests": [], "sponsorshipPolicy": sp, "bundlePolicy": bundle_policy()}

    def test_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(sp=bad):
                err = error_of(plan_bundle(self.doc_with(bad)))
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], "")

    def test_missing_key(self):
        err = error_of(plan_bundle(self.doc_with({"maxTotalGas": "0x1"})))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_unknown_key(self):
        sp = sponsorship_policy()
        sp["maxGas"] = "0x1"
        err = error_of(plan_bundle(self.doc_with(sp)))
        self.assertEqual(err["code"], E_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/maxGas")

    def test_zero_value(self):
        err = error_of(
            plan_bundle(self.doc_with({"budgetWei": "0x0", "maxTotalGas": "0x1"}))
        )
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_bad_quantity(self):
        sp = sponsorship_policy()
        sp["maxTotalGas"] = "100"
        err = error_of(plan_bundle(self.doc_with(sp)))
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")

    def test_missing_checked_before_unknown(self):
        err = error_of(
            plan_bundle(self.doc_with({"maxTotalGas": "0x1", "extra": "0x1"}))
        )
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")


class TestBundlePolicyValidation(unittest.TestCase):
    def doc_with(self, bp):
        return {"requests": [], "sponsorshipPolicy": sponsorship_policy(),
                "bundlePolicy": bp}

    def test_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(bp=bad):
                err = error_of(plan_bundle(self.doc_with(bad)))
                self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], "")

    def test_missing_first_key(self):
        err = error_of(plan_bundle(self.doc_with({"maxCostWei": "0x1"})))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")

    def test_missing_second_key(self):
        err = error_of(plan_bundle(self.doc_with({"maxTotalGas": "0x1"})))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/maxCostWei")

    def test_unknown_key(self):
        bp = bundle_policy()
        bp["budgetWei"] = "0x1"
        err = error_of(plan_bundle(self.doc_with(bp)))
        self.assertEqual(err["code"], E_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/budgetWei")

    def test_zero_max_total_gas(self):
        err = error_of(
            plan_bundle(self.doc_with({"maxTotalGas": "0x0", "maxCostWei": "0x1"}))
        )
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")

    def test_zero_max_cost(self):
        err = error_of(
            plan_bundle(self.doc_with({"maxTotalGas": "0x1", "maxCostWei": "0x0"}))
        )
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/maxCostWei")

    def test_non_quantity_value(self):
        err = error_of(
            plan_bundle(self.doc_with({"maxTotalGas": 5, "maxCostWei": "0x1"}))
        )
        self.assertEqual(err["code"], E_POLICY_INVALID_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")

    def test_missing_checked_before_unknown(self):
        err = error_of(
            plan_bundle(self.doc_with({"maxCostWei": "0x1", "extra": "0x1"}))
        )
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/maxTotalGas")

    def test_sponsorship_policy_validated_before_bundle_policy(self):
        # 两个策略都坏：报 sponsorshipPolicy 的缺键。
        doc = {"requests": [], "sponsorshipPolicy": {}, "bundlePolicy": {}}
        err = error_of(plan_bundle(doc))
        self.assertEqual(err["code"], E_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/budgetWei")


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

    def test_valid_exit_0_single_json_doc(self):
        code, out = self.run_cli(json.dumps(document([valid_request()])))
        self.assertEqual(code, 0)
        docs = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(docs), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])

    def test_empty_selection_exit_0(self):
        code, out = self.run_cli(json.dumps(document([])))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["plan"]["selected"], [])

    def test_sponsorship_rejected_still_exit_0(self):
        req = valid_request()
        sp = {"budgetWei": "0x1", "maxTotalGas": "0x1"}
        code, out = self.run_cli(json.dumps(document([req], sp=sp)))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["skipped"][0]["reason"], "E_GAS_LIMIT")

    def test_bundle_skipped_still_exit_0(self):
        code, out = self.run_cli(
            json.dumps(document([valid_request(), valid_request()],
                                bp=bundle_policy(max_total_gas=1)))
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_bad_request_exit_1(self):
        bad = valid_request()
        del bad["userOperation"]["sender"]
        code, out = self.run_cli(json.dumps(document([bad])))
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(out)["error"]["path"], "/requests/0/userOperation/sender"
        )

    def test_bad_policy_exit_1(self):
        doc = document([])
        doc["bundlePolicy"] = {}
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_POLICY_MISSING_FIELD)

    def test_malformed_json_exit_1(self):
        code, out = self.run_cli("{not json")
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["error"]["code"], E_INVALID_JSON)
        self.assertEqual(result["error"]["path"], "")

    def test_non_object_document_exit_1(self):
        code, out = self.run_cli("[1, 2]")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)


if __name__ == "__main__":
    unittest.main()
