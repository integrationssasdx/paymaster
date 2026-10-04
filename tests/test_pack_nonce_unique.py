"""paymaster.plan_bundle_nonce_unique 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_nonce_unique -v
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

from paymaster.packing import (  # noqa: E402
    _choose_nonce_unique,
    plan_bundle_nonce_unique,
)
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

SENDER_A = "0x1111111111111111111111111111111111111111"
SENDER_B = "0x2222222222222222222222222222222222222222"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# 标准单笔请求的 totalGas = 0x10000 + 0x20000 + 0x30000 = 393216
ITEM_GAS = 393216
# 标准单笔请求的 estimatedCostWei = 393216 * 0x10 = 6291456
ITEM_COST = 6291456


def make_request(
    sender: str = SENDER_A,
    nonce: int = 0,
    call_gas: int = 0x10000,
    max_fee: int = 0x10,
) -> dict:
    return {
        "userOperation": {
            "sender": sender,
            "nonce": hex(nonce),
            "callData": "0x",
            "callGasLimit": hex(call_gas),
            "verificationGasLimit": "0x20000",
            "preVerificationGas": "0x30000",
            "maxFeePerGas": hex(max_fee),
            "maxPriorityFeePerGas": "0x0",
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


class TestChooseNonceUnique(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, candidates, max_gas, max_cost):
        best_key = None
        best = None
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            keys = [candidates[i][0] for i in chosen]
            if len(set(keys)) != len(keys):
                continue
            gas = sum(candidates[i][1] for i in chosen)
            cost = sum(candidates[i][2] for i in chosen)
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
        self.assertEqual(_choose_nonce_unique([], 10, 10), ((), 0, 0))

    def test_conflict_picks_better_item(self):
        # 同 (sender, nonce) 两项只能取其一，取 gas 较小者。
        candidates = [(("s", 1), 5, 1), (("s", 1), 3, 9)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 10, 10)
        self.assertEqual((chosen, gas, cost), ((1,), 3, 9))

    def test_same_sender_different_nonce_no_conflict(self):
        candidates = [(("s", 1), 2, 2), (("s", 2), 2, 2)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 4, 4)
        self.assertEqual((chosen, gas, cost), ((0, 1), 4, 4))

    def test_different_sender_same_nonce_no_conflict(self):
        candidates = [(("a", 1), 2, 2), (("b", 1), 2, 2)]
        chosen, _, _ = _choose_nonce_unique(candidates, 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_conflict_group_not_counted_twice(self):
        # 组内三项 + 独立一项，容量足够全部，但组内只能取一个。
        candidates = [
            (("s", 1), 1, 1),
            (("s", 1), 1, 1),
            (("s", 1), 1, 1),
            (("t", 2), 1, 1),
        ]
        chosen, gas, cost = _choose_nonce_unique(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 3), 2, 2))

    def test_lexicographic_final_tiebreak(self):
        candidates = [
            (("a", 1), 2, 2),
            (("b", 1), 2, 2),
            (("c", 1), 2, 2),
        ]
        chosen, _, _ = _choose_nonce_unique(candidates, 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261004)
        for _ in range(2000):
            count = rng.randint(0, 9)
            candidates = [
                (
                    (rng.choice(["a", "b", "c"]), rng.randint(0, 2)),
                    rng.randint(1, 7),
                    rng.randint(1, 40),
                )
                for _ in range(count)
            ]
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_nonce_unique(candidates, max_gas, max_cost)
            want = self.brute_force(candidates, max_gas, max_cost)
            self.assertEqual(got, want, (candidates, max_gas, max_cost, got, want))


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_no_conflict_and_capacity_ample(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(
                    [make_request(nonce=0), make_request(nonce=1)],
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))

    def test_empty_requests_succeeds(self):
        plan = plan_of(
            plan_bundle_nonce_unique(document([], bundle_gas=1, bundle_cost=1))
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
            plan_bundle_nonce_unique(
                document([req], bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["operationCount"], "0")

    def test_same_sender_same_nonce_conflict(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(
                    [make_request(nonce=0), make_request(nonce=0)],
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )
        self.assertEqual(plan["operationCount"], "1")

    def test_same_sender_different_nonce_both_selected(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(
                    [make_request(nonce=0), make_request(nonce=1)],
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])

    def test_different_sender_same_nonce_both_selected(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(
                    [make_request(sender=SENDER_A, nonce=0),
                     make_request(sender=SENDER_B, nonce=0)],
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])

    def test_conflict_uses_normalized_sender_and_nonce(self):
        # sender 大小写不同、nonce 表示不同（0xa 与 0xA）仍视为同一组合。
        mixed = make_request(
            sender="0xaBcDeF000000000000000000000000000000cAfE", nonce=10
        )
        mixed["userOperation"]["nonce"] = "0xA"
        plain = make_request(
            sender="0xabcdef000000000000000000000000000000cafe", nonce=10
        )
        plan = plan_of(
            plan_bundle_nonce_unique(
                document([mixed, plain],
                         bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_conflict_checked_before_limits_for_excluded(self):
        # 1 号与入选的 0 号冲突；即使并入也会超 gas，仍记 E_NONCE_CONFLICT。
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(
                    [make_request(nonce=0), make_request(nonce=0),
                     make_request(nonce=1)],
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_conflict_reason_against_final_group(self):
        # 0 号入选后，1 号与之冲突；容量本身足够两项。
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(
                    [make_request(nonce=0), make_request(nonce=0),
                     make_request(nonce=1)],
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_conflict_group_picks_cheaper_item(self):
        # 同 (sender, nonce) 两项：gas 相同，成本较小者入选。
        pricey = make_request(nonce=0, max_fee=0x20)
        cheap = make_request(nonce=0, max_fee=0x10)
        plan = plan_of(
            plan_bundle_nonce_unique(
                document([pricey, cheap],
                         bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(
            plan["skipped"], [{"index": 0, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_single_oversized_request_marked_gas(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_single_overpriced_request_marked_budget(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document([make_request()], bundle_gas=ITEM_GAS,
                         bundle_cost=ITEM_COST - 1)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"],
                         [{"index": 0, "reason": "E_BUNDLE_BUDGET"}])

    def test_gas_checked_before_budget_for_excluded(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST - 1)
            )
        )
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_count_maximized_under_conflict(self):
        # 0 号大请求与 1、2 号小请求互不冲突；容量可容 0 号加一个小请求或两个
        # 小请求。最大数量方案为两个小请求。
        big = make_request(nonce=0, call_gas=0x20000)
        small_a = make_request(nonce=1, call_gas=0x1000)
        small_b = make_request(nonce=2, call_gas=0x1000)
        big_gas, big_cost = request_profile(big)
        small_gas, small_cost = request_profile(small_a)
        doc = document(
            [big, small_a, small_b],
            bundle_gas=big_gas + small_gas,  # big+一个小的放得下
            bundle_cost=big_cost + small_cost,
        )
        self.assertLess(small_gas * 2, big_gas + small_gas)
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])
        self.assertEqual(plan["operationCount"], "2")

    def test_conflict_group_drops_pricier_twin(self):
        # 0 号与 1 号冲突，2 号独立；容量足够任意两项。{0,2} 与 {1,2} 数量与
        # gas 相同，{1,2} 成本更小故胜；0 号与最终组有同 (sender, nonce) 项。
        pricey = make_request(nonce=0, max_fee=0x20)
        cheap = make_request(nonce=0, max_fee=0x10)
        other = make_request(nonce=1)
        doc = document(
            [pricey, cheap, other],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 3,
        )
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(
            plan["skipped"], [{"index": 0, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_unapproved_requests_keep_reason_and_position(self):
        gas_rejected = make_request(nonce=0)
        gas_rejected["userOperation"]["preVerificationGas"] = "0x200000"
        budget_rejected = make_request(nonce=1, max_fee=0xDE0B6B3A7640000)
        ok_req = make_request(nonce=2)
        doc = document(
            [gas_rejected, ok_req, budget_rejected],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_GAS_LIMIT"},
                {"index": 2, "reason": "E_BUDGET"},
            ],
        )

    def test_selected_and_skipped_sorted_and_strings_decimal(self):
        reqs = [make_request(nonce=i) for i in range(4)]
        doc = document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual([item["index"] for item in plan["skipped"]], [2, 3])
        for item in plan["skipped"]:
            self.assertEqual(set(item.keys()), {"index", "reason"})
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        doc = document(
            [make_request(nonce=0), make_request(nonce=0)],
            bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST,
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_nonce_unique(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_conflicts_or_violates_a_limit(self):
        """无冲突且两条限额均能装入的 approved 项必然入选（最大数量不变式）。"""
        rng = random.Random(7)
        for _ in range(50):
            count = rng.randint(1, 6)
            reqs = [
                make_request(
                    sender=rng.choice([SENDER_A, SENDER_B]),
                    nonce=rng.randint(0, 2),
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
            plan = plan_of(plan_bundle_nonce_unique(doc))
            selected = set(plan["selected"])
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            max_gas = int(doc["bundlePolicy"]["maxTotalGas"], 16)
            max_cost = int(doc["bundlePolicy"]["maxCostWei"], 16)
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            selected_keys = {
                (
                    reqs[i]["userOperation"]["sender"].lower(),
                    int(reqs[i]["userOperation"]["nonce"], 16),
                )
                for i in selected
            }
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                key = (
                    reqs[index]["userOperation"]["sender"].lower(),
                    int(reqs[index]["userOperation"]["nonce"], 16),
                )
                self.assertTrue(
                    key in selected_keys
                    or total_gas + gas > max_gas
                    or total_cost + cost > max_cost,
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
                err = error_of(plan_bundle_nonce_unique(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(plan_bundle_nonce_unique({"requests": [], "extra": 1}))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_nonce_unique({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_nonce_unique({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_nonce_unique({"requests": [], "sponsorshipPolicy": {}})
        )
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_nonce_unique(doc))
                self.assertEqual((err["code"], err["path"]),
                                 (E_INVALID_FIELD, "/requests"))

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request(nonce=1))
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_MISSING_FIELD, "/requests/1/userOperation/signature"))

    def test_request_root_error_path(self):
        doc = self.valid_doc()
        doc["requests"][0] = []
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_INVALID_JSON, "/requests/0"))

    def test_policy_errors(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = {}
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_MISSING_FIELD, "/bundlePolicy/maxTotalGas"))

        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"))

        doc = self.valid_doc()
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxTotalGas"))

    def test_result_shapes(self):
        result = plan_bundle_nonce_unique({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_nonce_unique(document([], bundle_gas=1, bundle_cost=1))
        self.assertTrue(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "plan"})
        self.assertEqual(
            set(result["plan"].keys()),
            {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"},
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_nonce_unique"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(nonce=0), make_request(nonce=0)],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])
        self.assertEqual(
            result["plan"]["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

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
