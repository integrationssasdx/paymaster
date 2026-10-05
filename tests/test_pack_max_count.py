"""paymaster.plan_bundle_max_count 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_max_count -v
"""

from __future__ import annotations

import copy
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paymaster.packing import _choose_max_count, plan_bundle_max_count  # noqa: E402
from paymaster.sponsorship import (  # noqa: E402
    E_POLICY_INVALID_FIELD,
    E_POLICY_MISSING_FIELD,
)
from paymaster.validation import (  # noqa: E402
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
)

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# 标准单笔请求的 totalGas = 0x10000 + 0x20000 + 0x30000 = 393216
ITEM_GAS = 393216
# 标准单笔请求的 estimatedCostWei = 393216 * 0x10 = 6291456
ITEM_COST = 6291456


def make_request(call_gas: int = 0x10000, max_fee: int = 0x10) -> dict:
    return {
        "userOperation": {
            "sender": SENDER,
            "nonce": "0x0",
            "callData": "0x",
            "callGasLimit": hex(call_gas),
            "verificationGasLimit": "0x20000",
            "preVerificationGas": "0x30000",
            "maxFeePerGas": hex(max_fee),
            # tip 与 cap 取相同值：baseFee(0x7) + tip >= maxFee，使
            # effectiveGasPriceWei = min(maxFee, baseFee + tip) 恰为 maxFee。
            "maxPriorityFeePerGas": hex(max_fee),
            "signature": "0x",
        },
        "context": {
            "version": "0.7",
            "chainId": "0x1",
            "entryPoint": ENTRY_POINT,
            "baseFeePerGas": "0x7",
        },
    }


def document(requests, *, bundle_gas: int, bundle_cost: int,
             sponsorship_gas: int = 0x100000,
             sponsorship_budget: int = 0xDE0B6B3A7640000) -> dict:
    return {
        "requests": requests,
        "sponsorshipPolicy": {
            "budgetWei": hex(sponsorship_budget),
            "maxTotalGas": hex(sponsorship_gas),
        },
        "bundlePolicy": {
            "maxTotalGas": hex(bundle_gas),
            "maxCostWei": hex(bundle_cost),
        },
    }


def plan_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result["plan"]


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


def request_profile(request: dict) -> tuple[int, int]:
    """单笔请求的 (totalGas, estimatedCostWei)。"""
    op = request["userOperation"]
    gas = (
        int(op["callGasLimit"], 16)
        + int(op["verificationGasLimit"], 16)
        + int(op["preVerificationGas"], 16)
    )
    return gas, gas * int(op["maxFeePerGas"], 16)


class TestChooseMaxCount(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, profiles, max_gas, max_cost):
        best_key = None
        best = None
        for mask in range(1 << len(profiles)):
            chosen = tuple(i for i in range(len(profiles)) if mask >> i & 1)
            gas = sum(profiles[i][0] for i in chosen)
            cost = sum(profiles[i][1] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            key = (-len(chosen), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        if best is None:
            return (), 0, 0
        return best

    def test_empty(self):
        self.assertEqual(_choose_max_count([], 10, 10), ((), 0, 0))

    def test_none_fit(self):
        chosen, gas, cost = _choose_max_count([(5, 5), (6, 1)], 4, 100)
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_simple_selection(self):
        chosen, gas, cost = _choose_max_count([(2, 100), (3, 4), (2, 4)], 5, 8)
        self.assertEqual(chosen, (1, 2))
        self.assertEqual((gas, cost), (5, 8))

    def test_count_outranks_metrics(self):
        # 单项方案 (gas=5,cost=1) 存在，但两项方案即便总量更大也应胜出；贵项
        # 0 号无法与任何项同组（成本超限）。
        chosen, _, _ = _choose_max_count([(1, 100), (5, 1), (5, 1)], 10, 2)
        self.assertEqual(chosen, (1, 2))

    def test_gas_tiebreak_before_cost(self):
        # 只能容一个：(gas=2,cost=9) 与 (gas=3,cost=1)，取 gas 小者。
        chosen, gas, cost = _choose_max_count([(2, 9), (3, 1)], 4, 20)
        self.assertEqual((chosen, gas, cost), ((0,), 2, 9))

    def test_cost_tiebreak(self):
        # gas 相同，取 cost 小者。
        chosen, gas, cost = _choose_max_count([(2, 5), (2, 3)], 2, 10)
        self.assertEqual((chosen, gas, cost), ((1,), 2, 3))

    def test_lexicographic_final_tiebreak(self):
        chosen, _, _ = _choose_max_count([(2, 2), (2, 2), (2, 2)], 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_zero_capacity(self):
        chosen, gas, cost = _choose_max_count([(1, 1)], 0, 0)
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261004)
        for _ in range(2000):
            count = rng.randint(0, 9)
            profiles = [(rng.randint(1, 7), rng.randint(1, 40)) for _ in range(count)]
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_max_count(profiles, max_gas, max_cost)
            want = self.brute_force(profiles, max_gas, max_cost)
            self.assertEqual(got, want, (profiles, max_gas, max_cost, got, want))


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_capacity_ample(self):
        plan = plan_of(
            plan_bundle_max_count(
                document([make_request(), make_request()],
                         bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))

    def test_empty_requests_succeeds(self):
        plan = plan_of(
            plan_bundle_max_count(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_no_approved_request_succeeds(self):
        req = make_request()
        req["userOperation"]["preVerificationGas"] = "0x200000"  # 超单笔 gas
        plan = plan_of(
            plan_bundle_max_count(
                document([req], bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["operationCount"], "0")

    def test_single_oversized_request_marked_gas(self):
        plan = plan_of(
            plan_bundle_max_count(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])
        self.assertEqual(plan["totalGas"], "0")

    def test_single_overpriced_request_marked_budget(self):
        plan = plan_of(
            plan_bundle_max_count(
                document([make_request()], bundle_gas=ITEM_GAS,
                         bundle_cost=ITEM_COST - 1)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_BUDGET"}])

    def test_gas_checked_before_budget_for_excluded(self):
        plan = plan_of(
            plan_bundle_max_count(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST - 1)
            )
        )
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_prefers_more_items_over_first_heavy_request(self):
        # 顺序贪心会先取 0 号大请求（仅 1 个）；最优解跳过它取两个小请求。
        big = make_request(call_gas=0x20000)
        small_a = make_request(call_gas=0x1000)
        small_b = make_request(call_gas=0x1000)
        big_gas, _ = request_profile(big)
        small_gas, small_cost = request_profile(small_a)
        doc = document(
            [big, small_a, small_b],
            bundle_gas=small_gas * 2,  # 两个小请求放得下，big+任一放不下
            bundle_cost=small_cost * 2 + 1,
        )
        self.assertGreater(big_gas + small_gas, small_gas * 2)
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(small_gas * 2))

    def test_count_tie_prefers_lower_total_gas(self):
        # 两个单项方案：0 号 gas 大但便宜，1 号 gas 小但贵；gas 较小者胜。
        heavy = make_request(call_gas=0x20000)  # gas 458752, cost 7340032
        light = make_request(call_gas=0x1000, max_fee=0x20)  # gas 331776
        heavy_gas, heavy_cost = request_profile(heavy)
        light_gas, light_cost = request_profile(light)
        doc = document(
            [heavy, light],
            bundle_gas=max(heavy_gas, light_gas),
            bundle_cost=max(heavy_cost, light_cost),
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_count_tie_prefers_lower_total_cost(self):
        # gas 相同、成本不同，只能容一个。
        pricey = make_request(call_gas=0x10000, max_fee=0x20)
        cheap = make_request(call_gas=0x10000, max_fee=0x10)
        gas, pricey_cost = request_profile(pricey)
        _, cheap_cost = request_profile(cheap)
        doc = document(
            [pricey, cheap],
            bundle_gas=gas,
            bundle_cost=max(pricey_cost, cheap_cost),
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1])  # 成本较小者胜
        # 0 号并入使 gas 超限 -> gas 先查 -> E_BUNDLE_GAS
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_lexicographic_final_tiebreak_end_to_end(self):
        reqs = [make_request(), make_request(), make_request()]
        doc = document(
            reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 3
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_BUNDLE_GAS"}])

    def test_excluded_budget_when_gas_fits_but_cost_does_not(self):
        # 两个极便宜的小请求入选；大请求并入后 gas 不超、成本超。
        cheap_small = make_request(call_gas=0x1000, max_fee=0x1)
        another = make_request(call_gas=0x1000, max_fee=0x1)
        pricey_big = make_request(call_gas=0x10000, max_fee=0x100)
        small_gas, small_cost = request_profile(cheap_small)
        big_gas, _ = request_profile(pricey_big)
        doc = document(
            [pricey_big, cheap_small, another],
            bundle_gas=small_gas * 2 + big_gas,  # gas 够三个
            bundle_cost=small_cost * 2,  # 成本只够两小
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_BUDGET"}])
        self.assertEqual(plan["estimatedCostWei"], str(small_cost * 2))

    def test_unapproved_requests_keep_reason_and_position(self):
        gas_rejected = make_request()
        gas_rejected["userOperation"]["preVerificationGas"] = "0x200000"
        budget_rejected = make_request(max_fee=0xDE0B6B3A7640000)
        ok_req = make_request()
        doc = document(
            [gas_rejected, ok_req, budget_rejected],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_GAS_LIMIT"},
                {"index": 2, "reason": "E_BUDGET"},
            ],
        )

    def test_selected_and_skipped_sorted_and_strings_decimal(self):
        doc = document(
            [make_request() for _ in range(4)],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(
            [item["index"] for item in plan["skipped"]], [2, 3]
        )
        for item in plan["skipped"]:
            self.assertEqual(set(item.keys()), {"index", "reason"})
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        doc = document(
            [make_request(), make_request()],
            bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST,
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_max_count(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_violates_a_limit(self):
        """不存在两项均未超却未入选的结果（最大数量不变式）。"""
        rng = random.Random(7)
        for _ in range(50):
            count = rng.randint(1, 6)
            reqs = [
                make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000, 0x20000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10, 0x40]),
                )
                for _ in range(count)
            ]
            profiles = [request_profile(r) for r in reqs]
            doc = document(
                reqs,
                bundle_gas=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                bundle_cost=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
            )
            plan = plan_of(plan_bundle_max_count(doc))
            selected = set(plan["selected"])
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            max_gas = int(doc["bundlePolicy"]["maxTotalGas"], 16)
            max_cost = int(doc["bundlePolicy"]["maxCostWei"], 16)
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                self.assertTrue(
                    total_gas + gas > max_gas or total_cost + cost > max_cost,
                    (doc, plan, index),
                )


class TestStructureErrorsParity(unittest.TestCase):
    """结构错误码、path 与检查顺序与 plan_bundle 完全一致。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10,
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle_max_count(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_max_count(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(plan_bundle_max_count({"requests": [], "extra": 1}))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_max_count({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_max_count({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_max_count({"requests": [], "sponsorshipPolicy": {}})
        )
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_max_count(doc))
                self.assertEqual((err["code"], err["path"]),
                                 (E_INVALID_FIELD, "/requests"))

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle_max_count(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_MISSING_FIELD, "/requests/1/userOperation/signature"))

    def test_request_root_error_path(self):
        doc = self.valid_doc()
        doc["requests"][0] = []
        err = error_of(plan_bundle_max_count(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_INVALID_JSON, "/requests/0"))

    def test_policy_errors(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = {}
        err = error_of(plan_bundle_max_count(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_MISSING_FIELD, "/bundlePolicy/maxTotalGas"))

        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_max_count(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"))

        doc = self.valid_doc()
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        err = error_of(plan_bundle_max_count(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxTotalGas"))

    def test_result_shapes(self):
        result = plan_bundle_max_count({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_max_count(document([], bundle_gas=1, bundle_cost=1))
        self.assertTrue(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "plan"})
        self.assertEqual(
            set(result["plan"].keys()),
            {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"},
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_max_count"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(), make_request()],
            bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 2,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])

    def test_empty_selection_exit_0(self):
        doc = document(
            [make_request()], bundle_gas=ITEM_GAS - 1, bundle_cost=ITEM_COST
        )
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(result["plan"]["skipped"],
                         [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_structural_failure_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["requests"][0]["userOperation"]["sender"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_MISSING_FIELD)

    def test_malformed_json_exit_1_empty_path(self):
        code, out, _ = self.run_cli("{not json")
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_INVALID_JSON)
        self.assertEqual(error["path"], "")

    def test_non_object_json_exit_1(self):
        code, out, _ = self.run_cli("[1, 2]")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)


if __name__ == "__main__":
    unittest.main()
