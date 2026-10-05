"""paymaster.plan_bundle_sender_budget_with_usage 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_sender_budget_with_usage -v
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

import paymaster  # noqa: E402
from paymaster.packing import (  # noqa: E402
    E_USAGE_INVALID_FIELD,
    _choose_sender_budget_with_usage,
    plan_bundle_sender_budget_with_usage,
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


def document(
    requests,
    *,
    bundle_gas: int,
    bundle_cost: int,
    max_gas_per_sender: int = 0x100000,
    max_cost_per_sender: int = 0xDE0B6B3A7640000,
    sponsorship_gas: int = 0x100000,
    sponsorship_budget: int = 0xDE0B6B3A7640000,
    usage: dict | None = None,
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
            "maxTotalGasPerSender": hex(max_gas_per_sender),
            "maxCostWeiPerSender": hex(max_cost_per_sender),
        },
        "senderUsage": usage if usage is not None else {},
    }


def usage_entry(gas: int, cost: int) -> dict:
    return {"totalGas": str(gas), "estimatedCostWei": str(cost)}


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


class TestChooseSenderBudgetWithUsage(unittest.TestCase):
    """直接对带累计用量的选择核心做穷举交叉验证。"""

    def brute_force(
        self, candidates, usage, max_gas_sender, max_cost_sender, max_gas, max_cost
    ):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            gas = sum(candidates[i][1] for i in chosen)
            cost = sum(candidates[i][2] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            per_gas: dict = {}
            per_cost: dict = {}
            for i in chosen:
                sender = candidates[i][0]
                if sender not in per_gas:
                    per_gas[sender], per_cost[sender] = usage.get(sender, (0, 0))
                per_gas[sender] += candidates[i][1]
                per_cost[sender] += candidates[i][2]
            if any(value > max_gas_sender for value in per_gas.values()):
                continue
            if any(value > max_cost_sender for value in per_cost.values()):
                continue
            batch_senders = {candidates[i][0] for i in chosen}
            key = (-len(chosen), -len(batch_senders), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        return best

    def test_empty(self):
        self.assertEqual(
            _choose_sender_budget_with_usage([], {"a": (3, 3)}, 1, 1, 10, 10),
            ((), 0, 0),
        )

    def test_usage_consumes_sender_allowance(self):
        # 无用量时同 sender 可入两项；用量占去余量后只能入一项。
        candidates = [("a", 2, 2), ("a", 2, 2)]
        chosen, _, _ = _choose_sender_budget_with_usage(
            candidates, {}, 4, 4, 100, 100
        )
        self.assertEqual(chosen, (0, 1))
        chosen, gas, cost = _choose_sender_budget_with_usage(
            candidates, {"a": (2, 1)}, 4, 4, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((0,), 2, 2))

    def test_usage_over_limit_blocks_sender_but_empty_selection_ok(self):
        chosen, gas, cost = _choose_sender_budget_with_usage(
            [("a", 1, 1)], {"a": (10, 10)}, 4, 4, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_usage_of_other_sender_does_not_count(self):
        chosen, _, _ = _choose_sender_budget_with_usage(
            [("a", 2, 2)], {"b": (100, 100)}, 2, 2, 100, 100
        )
        self.assertEqual(chosen, (0,))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261006)
        for _ in range(3000):
            count = rng.randint(0, 9)
            candidates = [
                (rng.choice("abc"), rng.randint(1, 7), rng.randint(1, 40))
                for _ in range(count)
            ]
            usage = {
                sender: (rng.randint(0, 15), rng.randint(0, 90))
                for sender in "abc"
                if rng.random() < 0.6
            }
            max_gas_sender = rng.randint(1, 20)
            max_cost_sender = rng.randint(1, 120)
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_sender_budget_with_usage(
                candidates,
                usage,
                max_gas_sender,
                max_cost_sender,
                max_gas,
                max_cost,
            )
            want = self.brute_force(
                candidates,
                usage,
                max_gas_sender,
                max_cost_sender,
                max_gas,
                max_cost,
            )
            self.assertEqual(
                got,
                want,
                (
                    candidates,
                    usage,
                    max_gas_sender,
                    max_cost_sender,
                    max_gas,
                    max_cost,
                ),
            )


class TestPlanEndToEnd(unittest.TestCase):
    def test_empty_usage_matches_sender_budget_baseline(self):
        doc = document(
            [make_request(), make_request(sender=SENDER_B)],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )
        plan = plan_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))

    def test_empty_requests_and_empty_usage_succeeds(self):
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document([], bundle_gas=1, bundle_cost=1)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_zero_usage_values_succeed(self):
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    [make_request()],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={SENDER_A: usage_entry(0, 0)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [])

    def test_usage_consumes_sender_gas_budget(self):
        reqs = [make_request(), make_request()]
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                    usage={SENDER_A: usage_entry(ITEM_GAS, 0)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_GAS"}])

    def test_usage_consumes_sender_cost_budget(self):
        reqs = [make_request(), make_request()]
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                    usage={SENDER_A: usage_entry(0, ITEM_COST)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_SENDER_COST"}]
        )

    def test_usage_alone_over_limit_yields_empty_selection(self):
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    [make_request()],
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS,
                    max_cost_per_sender=ITEM_COST,
                    usage={SENDER_A: usage_entry(1, 0)},
                )
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_SENDER_GAS"}])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")

    def test_usage_of_other_sender_does_not_count(self):
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    [make_request()],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_gas_per_sender=ITEM_GAS,
                    max_cost_per_sender=ITEM_COST,
                    usage={SENDER_B: usage_entry(ITEM_GAS * 100, ITEM_COST * 100)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [])

    def test_usage_key_compared_case_insensitively(self):
        letter_sender = "0x" + "ab" * 20
        reqs = [make_request(sender=letter_sender) for _ in range(2)]
        upper_usage = {"0x" + letter_sender[2:].upper(): usage_entry(ITEM_GAS, 0)}
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                    usage=upper_usage,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_GAS"}])

    def test_unapproved_request_does_not_consume_allowance(self):
        rejected = make_request()
        rejected["userOperation"]["preVerificationGas"] = "0x200000"
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    [rejected, make_request(sender=SENDER_B)],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_gas_per_sender=ITEM_GAS,
                    max_cost_per_sender=ITEM_COST,
                )
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])

    def test_bundle_limits_still_apply(self):
        reqs = [make_request(sender=SENDER_A), make_request(sender=SENDER_B)]
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST * 2,
                    usage={SENDER_A: usage_entry(1, 1)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_reason_order_sender_gas_first(self):
        # 用量使两条 sender 限额都容不下第二项：记 E_SENDER_GAS。
        reqs = [make_request(), make_request()]
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                    usage={SENDER_A: usage_entry(ITEM_GAS, ITEM_COST)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_GAS"}])

    def test_distinct_senders_still_preferred(self):
        # 容量恰够两项：{0,1} 同 sender，{0,2} 含两个 sender；用量不改变优先级。
        reqs = [
            make_request(sender=SENDER_A),
            make_request(sender=SENDER_A),
            make_request(sender=SENDER_B),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_usage(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                    usage={SENDER_C: usage_entry(5, 5)},
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_inputs_not_mutated(self):
        doc = document(
            [make_request(), make_request(sender=SENDER_B)],
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
            usage={SENDER_A: usage_entry(1, 2)},
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_sender_budget_with_usage(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_violates_first_limit_in_order(self):
        """未入选项必触某条限额（sender 聚合含既有用量），原因按 sender gas、
        sender cost、bundle gas、bundle budget 的次序取首个被突破者。"""
        rng = random.Random(17)
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
            usage = {
                sender: usage_entry(
                    rng.randint(0, ITEM_GAS), rng.randint(0, ITEM_COST)
                )
                for sender in senders
                if rng.random() < 0.5
            }
            doc = document(
                reqs,
                bundle_gas=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                bundle_cost=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
                max_gas_per_sender=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                max_cost_per_sender=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
                usage=usage,
            )
            plan = plan_of(plan_bundle_sender_budget_with_usage(doc))
            selected = set(plan["selected"])
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            max_gas = int(doc["bundlePolicy"]["maxTotalGas"], 16)
            max_cost = int(doc["bundlePolicy"]["maxCostWei"], 16)
            max_gas_sender = int(
                doc["senderBudgetPolicy"]["maxTotalGasPerSender"], 16
            )
            max_cost_sender = int(
                doc["senderBudgetPolicy"]["maxCostWeiPerSender"], 16
            )
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            per_sender_gas = {
                sender.lower(): int(entry["totalGas"])
                for sender, entry in usage.items()
            }
            per_sender_cost = {
                sender.lower(): int(entry["estimatedCostWei"])
                for sender, entry in usage.items()
            }
            for index in selected:
                sender = reqs[index]["userOperation"]["sender"].lower()
                gas, cost = profiles[index]
                per_sender_gas[sender] = per_sender_gas.get(sender, 0) + gas
                per_sender_cost[sender] = per_sender_cost.get(sender, 0) + cost
                self.assertLessEqual(per_sender_gas[sender], max_gas_sender)
                self.assertLessEqual(per_sender_cost[sender], max_cost_sender)
            skipped_reasons = {
                item["index"]: item["reason"] for item in plan["skipped"]
            }
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                sender = reqs[index]["userOperation"]["sender"].lower()
                over_sender_gas = (
                    per_sender_gas.get(sender, 0) + gas > max_gas_sender
                )
                over_sender_cost = (
                    per_sender_cost.get(sender, 0) + cost > max_cost_sender
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
                if over_sender_gas:
                    expected = "E_SENDER_GAS"
                elif over_sender_cost:
                    expected = "E_SENDER_COST"
                elif over_gas:
                    expected = "E_BUNDLE_GAS"
                else:
                    expected = "E_BUNDLE_BUDGET"
                self.assertEqual(
                    skipped_reasons[index], expected, (doc, plan, index)
                )


class TestSenderUsageValidation(unittest.TestCase):
    """senderUsage 的结构与字段校验：一律 E_USAGE_INVALID_FIELD。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def assert_usage_error(self, doc, path):
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(err["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(err["path"], path)

    def test_usage_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(usage=bad):
                doc = self.valid_doc()
                doc["senderUsage"] = bad
                self.assert_usage_error(doc, "/senderUsage")

    def test_empty_usage_object_ok(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {}
        self.assertTrue(plan_bundle_sender_budget_with_usage(doc)["ok"])

    def test_invalid_address_keys(self):
        for bad_key in (
            "0x1234",
            "0x" + "1" * 41,
            "1111111111111111111111111111111111111111",
            "0x" + "g" * 40,
            "0x" + "0" * 40,  # 零地址
            "0X" + "1" * 40,
            "",
        ):
            with self.subTest(key=bad_key):
                doc = self.valid_doc()
                doc["senderUsage"] = {bad_key: usage_entry(0, 0)}
                self.assert_usage_error(doc, "/senderUsage/" + bad_key)

    def test_mixed_case_address_key_ok(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {
            "0x" + "aB" * 20: usage_entry(1, 2),
            SENDER_B: usage_entry(3, 4),
        }
        self.assertTrue(plan_bundle_sender_budget_with_usage(doc)["ok"])

    def test_duplicate_key_after_lowercasing(self):
        letter_sender = "0x" + "ab" * 20
        doc = self.valid_doc()
        doc["senderUsage"] = {
            letter_sender: usage_entry(0, 0),
            "0x" + letter_sender[2:].upper(): usage_entry(0, 0),
        }
        self.assert_usage_error(
            doc, "/senderUsage/" + "0x" + letter_sender[2:].upper()
        )

    def test_entry_not_object(self):
        for bad in ([], "x", 1, None):
            with self.subTest(entry=bad):
                doc = self.valid_doc()
                doc["senderUsage"] = {SENDER_A: bad}
                self.assert_usage_error(doc, "/senderUsage/" + SENDER_A)

    def test_entry_missing_fields(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {SENDER_A: {"estimatedCostWei": "0"}}
        self.assert_usage_error(doc, "/senderUsage/" + SENDER_A + "/totalGas")

        doc = self.valid_doc()
        doc["senderUsage"] = {SENDER_A: {"totalGas": "0"}}
        self.assert_usage_error(
            doc, "/senderUsage/" + SENDER_A + "/estimatedCostWei"
        )

    def test_entry_unknown_field(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {SENDER_A: {**usage_entry(0, 0), "extra": "0"}}
        self.assert_usage_error(doc, "/senderUsage/" + SENDER_A + "/extra")

    def test_entry_missing_checked_before_unknown(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {SENDER_A: {"extra": "0"}}
        self.assert_usage_error(doc, "/senderUsage/" + SENDER_A + "/totalGas")

    def test_invalid_decimal_values(self):
        for bad in ("01", "-1", "0x10", "", " 1", "1 ", "1.0", "+1", 1, 0, None, True, [], {}):
            with self.subTest(value=bad):
                doc = self.valid_doc()
                doc["senderUsage"] = {
                    SENDER_A: {"totalGas": bad, "estimatedCostWei": "0"}
                }
                self.assert_usage_error(doc, "/senderUsage/" + SENDER_A + "/totalGas")

    def test_invalid_estimated_cost_value(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {
            SENDER_A: {"totalGas": "10", "estimatedCostWei": "007"}
        }
        self.assert_usage_error(
            doc, "/senderUsage/" + SENDER_A + "/estimatedCostWei"
        )

    def test_large_values_accepted(self):
        doc = self.valid_doc()
        big = str(10**40)
        doc["senderUsage"] = {SENDER_B: {"totalGas": big, "estimatedCostWei": big}}
        self.assertTrue(plan_bundle_sender_budget_with_usage(doc)["ok"])

    def test_usage_checked_after_policies(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = []
        doc["senderUsage"] = []
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/bundlePolicy"),
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"] = {}
        doc["senderUsage"] = {"bad-key": {}}
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_MISSING_FIELD, "/senderBudgetPolicy/maxTotalGasPerSender"),
        )


class TestStructureErrorsParity(unittest.TestCase):
    """根结构错误码、path 与检查顺序与 plan_bundle_sender_budget 一致。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle_sender_budget_with_usage(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(
            plan_bundle_sender_budget_with_usage({"requests": [], "extra": 1})
        )
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_sender_budget_with_usage({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_sender_budget_with_usage({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_sender_budget_with_usage(
                {"requests": [], "sponsorshipPolicy": {}}
            )
        )
        self.assertEqual(err["path"], "/bundlePolicy")
        err = error_of(
            plan_bundle_sender_budget_with_usage(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                }
            )
        )
        self.assertEqual(err["path"], "/senderBudgetPolicy")
        err = error_of(
            plan_bundle_sender_budget_with_usage(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                    "senderBudgetPolicy": {},
                }
            )
        )
        self.assertEqual(err["path"], "/senderUsage")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_sender_budget_with_usage(doc))
                self.assertEqual(
                    (err["code"], err["path"]),
                    (E_INVALID_FIELD, "/requests"),
                )

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_MISSING_FIELD, "/requests/1/userOperation/signature"),
        )

    def test_policy_errors_unchanged(self):
        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"),
        )

        doc = self.valid_doc()
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        err = error_of(plan_bundle_sender_budget_with_usage(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxTotalGas"),
        )

    def test_result_shapes(self):
        result = plan_bundle_sender_budget_with_usage({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_sender_budget_with_usage(
            document([], bundle_gas=1, bundle_cost=1)
        )
        self.assertTrue(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "plan"})
        self.assertEqual(
            set(result["plan"].keys()),
            {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"},
        )

    def test_exported_from_package(self):
        self.assertIs(
            paymaster.plan_bundle_sender_budget_with_usage,
            plan_bundle_sender_budget_with_usage,
        )
        self.assertIn("plan_bundle_sender_budget_with_usage", paymaster.__all__)


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_sender_budget_with_usage"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(), make_request(sender=SENDER_B)],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
            usage={SENDER_A: usage_entry(1, 1)},
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1])

    def test_usage_blocks_selection_exit_0(self):
        doc = document(
            [make_request()],
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
            max_gas_per_sender=ITEM_GAS,
            max_cost_per_sender=ITEM_COST,
            usage={SENDER_A: usage_entry(ITEM_GAS, 0)},
        )
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["plan"]["skipped"],
            [{"index": 0, "reason": "E_SENDER_GAS"}],
        )

    def test_usage_error_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["senderUsage"] = {SENDER_A: {"totalGas": "01", "estimatedCostWei": "0"}}
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(error["path"], "/senderUsage/" + SENDER_A + "/totalGas")

    def test_missing_sender_usage_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["senderUsage"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/senderUsage")

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
