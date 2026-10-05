"""paymaster.plan_bundle_sender_budget 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_sender_budget -v
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
    _choose_sender_budget,
    plan_bundle_sender_budget,
)
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

SENDER_A = "0x1111111111111111111111111111111111111111"
SENDER_B = "0x2222222222222222222222222222222222222222"
SENDER_C = "0x3333333333333333333333333333333333333333"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# 标准单笔请求的 totalGas = 0x10000 + 0x20000 + 0x30000 = 393216
ITEM_GAS = 393216
# 标准单笔请求的 estimatedCostWei = 393216 * 0x10 = 6291456
ITEM_COST = 6291456


def make_request(
    call_gas: int = 0x10000, max_fee: int = 0x10, sender: str = SENDER_A
) -> dict:
    return {
        "userOperation": {
            "sender": sender,
            "nonce": "0x0",
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


def document(
    requests,
    *,
    bundle_gas: int,
    bundle_cost: int,
    sender_gas: int = 0x100000,
    sender_cost: int = 0x10000000,
    sponsorship_gas: int = 0x100000,
    sponsorship_budget: int = 0xDE0B6B3A7640000,
) -> dict:
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
        "senderBudgetPolicy": {
            "maxTotalGasPerSender": hex(sender_gas),
            "maxCostWeiPerSender": hex(sender_cost),
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


class TestChooseSenderBudget(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, candidates, sender_gas_cap, sender_cost_cap,
                    max_gas, max_cost):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(
                i for i in range(len(candidates)) if mask >> i & 1
            )
            gas = sum(candidates[i][1] for i in chosen)
            cost = sum(candidates[i][2] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            per_gas: dict = {}
            per_cost: dict = {}
            for i in chosen:
                sender = candidates[i][0]
                per_gas[sender] = per_gas.get(sender, 0) + candidates[i][1]
                per_cost[sender] = per_cost.get(sender, 0) + candidates[i][2]
                if (
                    per_gas[sender] > sender_gas_cap
                    or per_cost[sender] > sender_cost_cap
                ):
                    break
            else:
                key = (-len(chosen), -len(per_gas), gas, cost, chosen)
                if best_key is None or key < best_key:
                    best_key = key
                    best = (chosen, gas, cost)
        return best

    def test_empty(self):
        self.assertEqual(_choose_sender_budget([], 10, 10, 10, 10), ((), 0, 0))

    def test_none_fit(self):
        chosen, gas, cost = _choose_sender_budget(
            [("a", 5, 5)], 4, 100, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_sender_gas_caps_same_sender(self):
        candidates = [("a", 2, 2), ("a", 2, 2), ("a", 2, 2)]
        chosen, gas, cost = _choose_sender_budget(
            candidates, 4, 100, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((0, 1), 4, 4))

    def test_sender_cost_caps_same_sender(self):
        candidates = [("a", 2, 3), ("a", 2, 3), ("a", 2, 3)]
        chosen, gas, cost = _choose_sender_budget(
            candidates, 100, 6, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((0, 1), 4, 6))

    def test_distinct_senders_outrank_metrics(self):
        # 数量并列 2：{0,1} 总量更小但只含 1 个 sender；{0,2} 含 2 个 sender。
        candidates = [("a", 1, 1), ("a", 1, 1), ("b", 9, 9)]
        chosen, _, _ = _choose_sender_budget(
            candidates, 100, 100, 10, 10
        )
        self.assertEqual(chosen, (0, 2))

    def test_gas_tiebreak_after_senders(self):
        # 数量与 sender 数都并列：取总 gas 较小者。
        candidates = [("a", 3, 1), ("b", 4, 1), ("c", 5, 1)]
        chosen, gas, _ = _choose_sender_budget(
            candidates, 100, 100, 5, 10
        )
        self.assertEqual((chosen, gas), ((0,), 3))

    def test_cost_tiebreak(self):
        candidates = [("a", 2, 5), ("b", 2, 3)]
        chosen, _, cost = _choose_sender_budget(
            candidates, 100, 100, 2, 10
        )
        self.assertEqual((chosen, cost), ((1,), 3))

    def test_lexicographic_final_tiebreak(self):
        candidates = [("a", 2, 2), ("b", 2, 2), ("c", 2, 2)]
        chosen, _, _ = _choose_sender_budget(
            candidates, 100, 100, 4, 4
        )
        self.assertEqual(chosen, (0, 1))

    def test_zero_capacity(self):
        chosen, gas, cost = _choose_sender_budget(
            [("a", 1, 1)], 100, 100, 0, 0
        )
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261005)
        for _ in range(2000):
            count = rng.randint(0, 9)
            candidates = [
                (rng.choice("abc"), rng.randint(1, 7), rng.randint(1, 40))
                for _ in range(count)
            ]
            sender_gas_cap = rng.randint(1, 20)
            sender_cost_cap = rng.randint(1, 120)
            max_gas = rng.randint(0, 30)
            max_cost = rng.randint(0, 200)
            got = _choose_sender_budget(
                candidates,
                sender_gas_cap,
                sender_cost_cap,
                max_gas,
                max_cost,
            )
            want = self.brute_force(
                candidates,
                sender_gas_cap,
                sender_cost_cap,
                max_gas,
                max_cost,
            )
            self.assertEqual(
                got,
                want,
                (
                    candidates,
                    sender_gas_cap,
                    sender_cost_cap,
                    max_gas,
                    max_cost,
                ),
            )


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_capacity_ample(self):
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    [
                        make_request(sender=SENDER_A),
                        make_request(sender=SENDER_B),
                    ],
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
            plan_bundle_sender_budget(
                document([], bundle_gas=1, bundle_cost=1)
            )
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
            plan_bundle_sender_budget(
                document(
                    [req],
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                )
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["operationCount"], "0")

    def test_same_sender_gas_cap_partial_selection(self):
        # 同一 sender 三项，per-sender gas 只够两项，bundle 容量充足。
        reqs = [make_request() for _ in range(3)]
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    sender_gas=ITEM_GAS * 2,
                    sender_cost=ITEM_COST * 3,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_SENDER_GAS"}])
        self.assertEqual(plan["operationCount"], "2")

    def test_same_sender_cost_cap_partial_selection(self):
        # 同一 sender 三项，per-sender cost 只够两项，gas 充足。
        reqs = [make_request() for _ in range(3)]
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    sender_gas=ITEM_GAS * 3,
                    sender_cost=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_SENDER_COST"}])

    def test_sender_address_compared_case_insensitively(self):
        lower = make_request(sender=SENDER_B.lower())
        upper = make_request(sender="0x" + SENDER_B[2:].upper())
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    [lower, upper],
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    sender_gas=ITEM_GAS,
                    sender_cost=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_GAS"}])

    def test_sender_gas_reason_before_sender_cost_and_bundle(self):
        # 同一 sender 第二项同时突破 sender gas、sender cost 与 bundle gas，
        # 只记最先检查的 E_SENDER_GAS。
        reqs = [make_request(), make_request()]
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    sender_gas=ITEM_GAS,
                    sender_cost=ITEM_COST,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_GAS"}])

    def test_sender_cost_reason_before_bundle(self):
        # 同一 sender 第二项 gas 不超 sender 限额，但 sender 成本与 bundle
        # 成本都超限：记 E_SENDER_COST。
        cheap = make_request(max_fee=0x1)
        pricey = make_request(max_fee=0x10)
        cheap_gas, cheap_cost = request_profile(cheap)
        pricey_gas, pricey_cost = request_profile(pricey)
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    [cheap, pricey],
                    bundle_gas=cheap_gas + pricey_gas,
                    bundle_cost=cheap_cost,
                    sender_gas=cheap_gas + pricey_gas,
                    sender_cost=cheap_cost,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_COST"}])

    def test_bundle_gas_reason_when_sender_caps_ample(self):
        # 两个不同 sender 各一项，per-sender 限额充足，bundle gas 只够一项。
        reqs = [make_request(sender=SENDER_A), make_request(sender=SENDER_B)]
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_bundle_budget_reason_when_only_cost_blocks(self):
        cheap = make_request(max_fee=0x1, sender=SENDER_A)
        pricey = make_request(max_fee=0x100, sender=SENDER_B)
        cheap_gas, cheap_cost = request_profile(cheap)
        pricey_gas, pricey_cost = request_profile(pricey)
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    [cheap, pricey],
                    bundle_gas=cheap_gas + pricey_gas,
                    bundle_cost=cheap_cost,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_BUDGET"}])

    def test_distinct_senders_preferred_end_to_end(self):
        # 容量恰够两项：{0,1} 同 sender，{0,2} 与 {1,2} 含两个 sender。
        reqs = [
            make_request(sender=SENDER_A),
            make_request(sender=SENDER_A),
            make_request(sender=SENDER_B),
        ]
        plan = plan_of(
            plan_bundle_sender_budget(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    sender_gas=ITEM_GAS * 2,
                    sender_cost=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        # 1 号所在 sender 的 gas 预算未用尽（只入选 0 号），并入使 bundle gas
        # 超限。
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_unapproved_requests_keep_reason_and_position(self):
        gas_rejected = make_request(sender=SENDER_A)
        gas_rejected["userOperation"]["preVerificationGas"] = "0x200000"
        budget_rejected = make_request(
            max_fee=0xDE0B6B3A7640000, sender=SENDER_B
        )
        ok_req = make_request(sender=SENDER_C)
        doc = document(
            [gas_rejected, ok_req, budget_rejected],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_sender_budget(doc))
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
            bundle_gas=ITEM_GAS * 4,
            bundle_cost=ITEM_COST * 4,
            sender_gas=ITEM_GAS * 2,
            sender_cost=ITEM_COST * 4,
        )
        plan = plan_of(plan_bundle_sender_budget(doc))
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
            [make_request(), make_request(sender=SENDER_B)],
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
            sender_gas=ITEM_GAS,
            sender_cost=ITEM_COST,
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_sender_budget(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_violates_a_constraint(self):
        """未入选项必触一条限额，且原因与检查次序一致（最大数量不变式）。"""
        rng = random.Random(11)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(80):
            count = rng.randint(1, 6)
            reqs = [
                make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000, 0x20000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10, 0x40]),
                    sender=rng.choice(senders),
                )
                for _ in range(count)
            ]
            profiles = [request_profile(r) for r in reqs]
            doc = document(
                reqs,
                bundle_gas=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                bundle_cost=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
                sender_gas=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                sender_cost=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
            )
            plan = plan_of(plan_bundle_sender_budget(doc))
            selected = set(plan["selected"])
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            max_gas = int(doc["bundlePolicy"]["maxTotalGas"], 16)
            max_cost = int(doc["bundlePolicy"]["maxCostWei"], 16)
            cap_gas = int(
                doc["senderBudgetPolicy"]["maxTotalGasPerSender"], 16
            )
            cap_cost = int(
                doc["senderBudgetPolicy"]["maxCostWeiPerSender"], 16
            )
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            sender_gas: dict[str, int] = {}
            sender_cost: dict[str, int] = {}
            for index in selected:
                sender = reqs[index]["userOperation"]["sender"].lower()
                gas, cost = profiles[index]
                sender_gas[sender] = sender_gas.get(sender, 0) + gas
                sender_cost[sender] = sender_cost.get(sender, 0) + cost
                self.assertLessEqual(sender_gas[sender], cap_gas)
                self.assertLessEqual(sender_cost[sender], cap_cost)
            skipped_reasons = {
                item["index"]: item["reason"] for item in plan["skipped"]
            }
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                sender = reqs[index]["userOperation"]["sender"].lower()
                over_sender_gas = (
                    sender_gas.get(sender, 0) + gas > cap_gas
                )
                over_sender_cost = (
                    sender_cost.get(sender, 0) + cost > cap_cost
                )
                over_gas = total_gas + gas > max_gas
                over_cost = total_cost + cost > max_cost
                self.assertTrue(
                    over_sender_gas
                    or over_sender_cost
                    or over_gas
                    or over_cost,
                    (doc, plan, index),
                )
                reason = skipped_reasons[index]
                if over_sender_gas:
                    self.assertEqual(reason, "E_SENDER_GAS")
                elif over_sender_cost:
                    self.assertEqual(reason, "E_SENDER_COST")
                elif over_gas:
                    self.assertEqual(reason, "E_BUNDLE_GAS")
                else:
                    self.assertEqual(reason, "E_BUNDLE_BUDGET")


class TestStructureErrorsParity(unittest.TestCase):
    """结构错误码、path 与检查顺序与 plan_bundle 系一致。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle_sender_budget(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(
            plan_bundle_sender_budget(
                {"requests": [], "extra": 1, "sponsorshipPolicy": {},
                 "bundlePolicy": {}, "senderBudgetPolicy": {}}
            )
        )
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_sender_budget({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_sender_budget({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_sender_budget(
                {"requests": [], "sponsorshipPolicy": {}}
            )
        )
        self.assertEqual(err["path"], "/bundlePolicy")
        err = error_of(
            plan_bundle_sender_budget(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                }
            )
        )
        self.assertEqual(err["path"], "/senderBudgetPolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_sender_budget(doc))
                self.assertEqual(
                    (err["code"], err["path"]),
                    (E_INVALID_FIELD, "/requests"),
                )

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_MISSING_FIELD, "/requests/1/userOperation/signature"),
        )

    def test_request_root_error_path(self):
        doc = self.valid_doc()
        doc["requests"][0] = []
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]), (E_INVALID_JSON, "/requests/0")
        )

    def test_policy_errors(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = {}
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_MISSING_FIELD, "/bundlePolicy/maxTotalGas"),
        )

        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"),
        )

    def test_sender_budget_policy_errors(self):
        doc = self.valid_doc()
        doc["senderBudgetPolicy"] = []
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/senderBudgetPolicy"),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"] = {}
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (
                E_POLICY_MISSING_FIELD,
                "/senderBudgetPolicy/maxTotalGasPerSender",
            ),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"] = {"maxTotalGasPerSender": "0x1"}
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (
                E_POLICY_MISSING_FIELD,
                "/senderBudgetPolicy/maxCostWeiPerSender",
            ),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"]["extra"] = "0x1"
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_UNKNOWN_FIELD, "/senderBudgetPolicy/extra"),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"]["maxTotalGasPerSender"] = "0x0"
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (
                E_POLICY_INVALID_FIELD,
                "/senderBudgetPolicy/maxTotalGasPerSender",
            ),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"]["maxCostWeiPerSender"] = "2"
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (
                E_POLICY_INVALID_FIELD,
                "/senderBudgetPolicy/maxCostWeiPerSender",
            ),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"]["maxTotalGasPerSender"] = 1
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (
                E_POLICY_INVALID_FIELD,
                "/senderBudgetPolicy/maxTotalGasPerSender",
            ),
        )

    def test_sender_budget_policy_checked_after_bundle_policy(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = []
        doc["senderBudgetPolicy"] = []
        err = error_of(plan_bundle_sender_budget(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/bundlePolicy"),
        )

    def test_result_shapes(self):
        result = plan_bundle_sender_budget({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_sender_budget(
            document([], bundle_gas=1, bundle_cost=1)
        )
        self.assertTrue(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "plan"})
        self.assertEqual(
            set(result["plan"].keys()),
            {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"},
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_sender_budget"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(sender=SENDER_A), make_request(sender=SENDER_B)],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
            sender_gas=ITEM_GAS,
            sender_cost=ITEM_COST,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1])

    def test_empty_selection_exit_0(self):
        doc = document(
            [make_request()],
            bundle_gas=ITEM_GAS - 1,
            bundle_cost=ITEM_COST,
        )
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["plan"]["skipped"],
            [{"index": 0, "reason": "E_BUNDLE_GAS"}],
        )

    def test_structural_failure_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["requests"][0]["userOperation"]["sender"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_MISSING_FIELD)

    def test_missing_sender_budget_policy_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["senderBudgetPolicy"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/senderBudgetPolicy")

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
