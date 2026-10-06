"""paymaster.plan_batch_sequence_sender_fair 与其 CLI 的单元测试。

运行：python -m unittest tests.test_batch_sequence_sender_fair -v
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
from paymaster.batch_sequence import plan_batch_sequence  # noqa: E402
from paymaster.batch_sequence_sender_fair import (  # noqa: E402
    E_BATCH_POLICY_INVALID_FIELD,
    E_BATCH_POLICY_MISSING_FIELD,
    E_FAIRNESS_POLICY_INVALID_FIELD,
    E_FAIRNESS_POLICY_MISSING_FIELD,
    E_FAIRNESS_POLICY_UNKNOWN_FIELD,
    plan_batch_sequence_sender_fair,
)
from paymaster.packing import (  # noqa: E402
    E_NONCE_STATE_INVALID_FIELD,
    E_USAGE_INVALID_FIELD,
)
from paymaster.sponsorship import evaluate_sponsorship  # noqa: E402
from paymaster.validation import (  # noqa: E402
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
    validate,
)

SENDER_A = "0x1111111111111111111111111111111111111111"
SENDER_B = "0x2222222222222222222222222222222222222222"
SENDER_C = "0x3333333333333333333333333333333333333333"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# 标准单笔请求的 totalGas = 0x10000 + 0x20000 + 0x30000 = 393216
ITEM_GAS = 393216
# 标准单笔请求的 estimatedCostWei = 393216 * 0x10 = 6291456
ITEM_COST = 6291456

NONCE_MOD = 1 << 64


def make_request(
    call_gas: int = 0x10000,
    max_fee: int = 0x10,
    sender: str = SENDER_A,
    nonce: int = 0,
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


def nonce_of(key: int, sequence: int) -> int:
    return key * NONCE_MOD + sequence


def document(
    requests,
    *,
    bundle_gas: int,
    bundle_cost: int,
    max_gas_per_sender: int = 0x1000000,
    max_cost_per_sender: int = 0xDE0B6B3A7640000,
    sponsorship_gas: int = 0x1000000,
    sponsorship_budget: int = 0xDE0B6B3A7640000,
    usage: dict | None = None,
    nonce_state: dict | None = None,
    max_batches: int = 4,
    max_ops: int = 4,
    max_per_sender_per_batch: int = 4,
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
        "senderNonceState": nonce_state if nonce_state is not None else {},
        "batchPolicy": {
            "maxBatchCount": hex(max_batches),
            "maxOperationsPerBatch": hex(max_ops),
        },
        "fairnessPolicy": {
            "maxPerSenderPerBatch": hex(max_per_sender_per_batch),
        },
    }


def seven_field_doc(doc: dict) -> dict:
    """去掉 fairnessPolicy，得到 plan_batch_sequence 的七字段文档。"""
    return {key: value for key, value in doc.items() if key != "fairnessPolicy"}


def usage_entry(gas: int, cost: int) -> dict:
    return {"totalGas": str(gas), "estimatedCostWei": str(cost)}


def state_entry(nonce_key: int, last_sequence: int) -> dict:
    return {"nonceKey": str(nonce_key), "lastSequence": str(last_sequence)}


def result_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


def brute_force_plan(doc: dict):
    """暴力枚举所有入选子集，返回 (selected, batches) 的最优解。

    与 tests.test_batch_sequence 的暴力枚举相同，另在贪心划分批次时叠加每批
    同一 sender 入选数不超过 maxPerSenderPerBatch。
    """
    requests = doc["requests"]
    decisions = [
        evaluate_sponsorship(request, doc["sponsorshipPolicy"])["decision"]
        for request in requests
    ]
    max_total_gas = int(doc["bundlePolicy"]["maxTotalGas"], 16)
    max_cost_wei = int(doc["bundlePolicy"]["maxCostWei"], 16)
    max_gps = int(doc["senderBudgetPolicy"]["maxTotalGasPerSender"], 16)
    max_cps = int(doc["senderBudgetPolicy"]["maxCostWeiPerSender"], 16)
    max_batches = int(doc["batchPolicy"]["maxBatchCount"], 16)
    max_ops = int(doc["batchPolicy"]["maxOperationsPerBatch"], 16)
    max_spb = int(doc["fairnessPolicy"]["maxPerSenderPerBatch"], 16)
    usage = {
        key.lower(): (int(entry["totalGas"]), int(entry["estimatedCostWei"]))
        for key, entry in doc["senderUsage"].items()
    }
    state = {
        sender: {
            int(entry["nonceKey"]): int(entry["lastSequence"])
            for entry in entries
        }
        for sender, entries in doc["senderNonceState"].items()
    }

    info = []  # (原下标, sender, key, sequence, gas, cost)
    for index, (request, decision) in enumerate(zip(requests, decisions)):
        if not decision["approved"]:
            continue
        user_op = validate(request)["normalized"]["userOperation"]
        nonce = int(user_op["nonce"], 16)
        info.append(
            (
                index,
                user_op["sender"],
                nonce // NONCE_MOD,
                nonce % NONCE_MOD,
                int(decision["totalGas"]),
                int(decision["estimatedCostWei"]),
            )
        )

    best_key = None
    best = ((), [])
    for mask in range(1 << len(info)):
        chosen = [info[j] for j in range(len(info)) if mask >> j & 1]
        # nonce 锚定链：每组入选 sequence 恰为 start..start+m-1（下标递增序）。
        groups: dict = {}
        for _, sender, key, sequence, _, _ in chosen:
            groups.setdefault((sender, key), []).append(sequence)
        feasible = True
        for (sender, key), sequences in groups.items():
            start = state.get(sender, {}).get(key, -1) + 1
            if sequences != list(range(start, start + len(sequences))):
                feasible = False
                break
        if not feasible:
            continue
        # sender 跨批聚合限额（含既有用量）。
        per_sender: dict = {}
        for _, sender, _, _, gas, cost in chosen:
            used_gas, used_cost = per_sender.get(sender, (0, 0))
            per_sender[sender] = (used_gas + gas, used_cost + cost)
        for sender, (gas, cost) in per_sender.items():
            prior_gas, prior_cost = usage.get(sender, (0, 0))
            if gas + prior_gas > max_gps or cost + prior_cost > max_cps:
                feasible = False
                break
        if not feasible:
            continue
        # 贪心最长前缀划分批次（叠加批内 sender 上限）。
        batches = []
        current = []
        fill_count = fill_gas = fill_cost = 0
        fill_senders: dict = {}
        for item in chosen:
            sender, gas, cost = item[1], item[4], item[5]
            sender_in_batch = fill_senders.get(sender, 0)
            if current and (
                fill_count + 1 > max_ops
                or fill_gas + gas > max_total_gas
                or fill_cost + cost > max_cost_wei
                or sender_in_batch + 1 > max_spb
            ):
                batches.append(current)
                current = []
                fill_count = fill_gas = fill_cost = 0
                fill_senders = {}
                sender_in_batch = 0
            current.append(item)
            fill_count += 1
            fill_gas += gas
            fill_cost += cost
            fill_senders[sender] = sender_in_batch + 1
        if current:
            batches.append(current)
        # 单项即超批限额时该子集不可行；其余情形贪心划分必满足限额。
        for batch in batches:
            if (
                len(batch) > max_ops
                or sum(item[4] for item in batch) > max_total_gas
                or sum(item[5] for item in batch) > max_cost_wei
                or any(
                    sum(1 for item in batch if item[1] == sender) > max_spb
                    for sender in {item[1] for item in batch}
                )
            ):
                feasible = False
                break
        if not feasible or len(batches) > max_batches:
            continue
        selected = tuple(item[0] for item in chosen)
        total_gas = sum(item[4] for item in chosen)
        total_cost = sum(item[5] for item in chosen)
        key = (
            -len(chosen),
            len(batches),
            -len(per_sender),
            total_gas,
            total_cost,
            selected,
        )
        if best_key is None or key < best_key:
            best_key = key
            best = (selected, [[item[0] for item in batch] for batch in batches])
    return best


class TestValidation(unittest.TestCase):
    """文档结构与 fairnessPolicy 校验。"""

    def valid_doc(self):
        return document(
            [make_request()], bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                error = error_of(plan_batch_sequence_sender_fair(bad))
                self.assertEqual(error["code"], E_INVALID_JSON)
                self.assertEqual(error["path"], "")

    def test_unknown_root_field(self):
        doc = self.valid_doc()
        doc["extra"] = 1
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_UNKNOWN_FIELD)
        self.assertEqual(error["path"], "/extra")

    def test_missing_root_fields(self):
        error = error_of(plan_batch_sequence_sender_fair({}))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/requests")
        doc = self.valid_doc()
        del doc["fairnessPolicy"]
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/fairnessPolicy")
        doc = self.valid_doc()
        del doc["batchPolicy"]
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/batchPolicy")

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        del doc["requests"][0]["userOperation"]["signature"]
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertTrue(error["path"].startswith("/requests/0/"))

    def test_fairness_policy_not_object(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"] = []
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_FAIRNESS_POLICY_INVALID_FIELD)
        self.assertEqual(error["path"], "/fairnessPolicy")

    def test_fairness_policy_missing_field(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"] = {}
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_FAIRNESS_POLICY_MISSING_FIELD)
        self.assertEqual(
            error["path"], "/fairnessPolicy/maxPerSenderPerBatch"
        )

    def test_fairness_policy_unknown_field(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"]["extra"] = "0x1"
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_FAIRNESS_POLICY_UNKNOWN_FIELD)
        self.assertEqual(error["path"], "/fairnessPolicy/extra")

    def test_fairness_policy_invalid_values(self):
        for bad in ("0x0", "0x01", "01", "1", "", 1, None, True, "0x"):
            doc = self.valid_doc()
            doc["fairnessPolicy"]["maxPerSenderPerBatch"] = bad
            with self.subTest(value=bad):
                error = error_of(plan_batch_sequence_sender_fair(doc))
                self.assertEqual(error["code"], E_FAIRNESS_POLICY_INVALID_FIELD)
                self.assertEqual(
                    error["path"], "/fairnessPolicy/maxPerSenderPerBatch"
                )

    def test_fairness_policy_upper_bound(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"]["maxPerSenderPerBatch"] = hex((1 << 64) - 1)
        self.assertTrue(plan_batch_sequence_sender_fair(doc)["ok"])
        doc["fairnessPolicy"]["maxPerSenderPerBatch"] = hex(1 << 64)
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_FAIRNESS_POLICY_INVALID_FIELD)
        self.assertEqual(
            error["path"], "/fairnessPolicy/maxPerSenderPerBatch"
        )

    def test_fairness_policy_checked_last(self):
        # batchPolicy 先于 fairnessPolicy。
        doc = self.valid_doc()
        doc["batchPolicy"] = []
        doc["fairnessPolicy"] = []
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_INVALID_FIELD)
        self.assertEqual(error["path"], "/batchPolicy")
        # senderUsage / senderNonceState 先于两个最后策略。
        doc = self.valid_doc()
        doc["senderUsage"] = []
        doc["batchPolicy"] = []
        doc["fairnessPolicy"] = []
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(error["path"], "/senderUsage")
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": "01", "lastSequence": "0"}]
        }
        doc["fairnessPolicy"] = []
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_NONCE_STATE_INVALID_FIELD)

    def test_batch_policy_still_validated(self):
        doc = self.valid_doc()
        doc["batchPolicy"] = {"maxBatchCount": "0x1"}
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/maxOperationsPerBatch")

    def test_error_result_shape(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"] = {}
        error = error_of(plan_batch_sequence_sender_fair(doc))
        self.assertEqual(set(error), {"code", "path", "message"})

    def test_exported_from_package(self):
        self.assertIs(
            paymaster.plan_batch_sequence_sender_fair,
            plan_batch_sequence_sender_fair,
        )
        self.assertIn("plan_batch_sequence_sender_fair", paymaster.__all__)


class TestPlanning(unittest.TestCase):
    """分批、批内 sender 上限、约束与目标优先级。"""

    def test_empty_requests(self):
        got = result_of(
            plan_batch_sequence_sender_fair(
                document([], bundle_gas=1, bundle_cost=1)
            )
        )
        self.assertEqual(
            got["plan"],
            {
                "selected": [],
                "skipped": [],
                "operationCount": "0",
                "totalGas": "0",
                "estimatedCostWei": "0",
                "batches": [],
            },
        )
        self.assertEqual(got["nextSenderUsage"], {})
        self.assertEqual(got["nextSenderNonceState"], {})
        self.assertEqual(
            set(got), {"ok", "plan", "nextSenderUsage", "nextSenderNonceState"}
        )

    def test_per_sender_per_batch_forces_split(self):
        # 同一 sender 的 nonce 链三项：一批可装三项且限额充足，但每批至多 1
        # 个该 sender，只有 2 批时入选两项。
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3)]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_batches=2,
                    max_ops=3,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]], [[0], [1]]
        )
        self.assertEqual(
            got["plan"]["skipped"], [{"index": 2, "reason": "E_NOT_SELECTED"}]
        )

    def test_per_sender_per_batch_with_three_batches(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3)]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_batches=3,
                    max_ops=3,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0], [1], [2]],
        )

    def test_interleaving_senders_fills_batch(self):
        # A, A, B：cap=1 时 A0 独占首批（批次为入选序列的连续分段，A1 位于
        # B2 之前，B2 不能回填首批），A1、B2 同处第二批，三项全部入选。
        reqs = [
            make_request(sender=SENDER_A, nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_A, nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_batches=2,
                    max_ops=3,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0], [1, 2]],
        )

    def test_cap_two_keeps_same_sender_together(self):
        reqs = [
            make_request(sender=SENDER_A, nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_A, nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_batches=2,
                    max_ops=3,
                    max_per_sender_per_batch=2,
                )
            )
        )
        # 三项同一批即可：1 批优先于其他并列方案。
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0, 1, 2]],
        )

    def test_cap_does_not_change_result_when_large(self):
        # cap 足够大时，结果必须与 plan_batch_sequence 完全一致。
        reqs = [
            make_request(
                call_gas=0x4000,
                max_fee=0x8,
                sender=(SENDER_A, SENDER_B, SENDER_C)[i % 3],
                nonce=nonce_of(i % 2, i),
            )
            for i in range(6)
        ]
        doc = document(
            reqs,
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
            max_batches=3,
            max_ops=2,
            max_per_sender_per_batch=10,
        )
        got = result_of(plan_batch_sequence_sender_fair(doc))["plan"]
        want = result_of(plan_batch_sequence(seven_field_doc(doc)))["plan"]
        self.assertEqual(got, want)

    def test_batch_summaries(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batches=2,
                    max_ops=1,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(len(got["plan"]["batches"]), 2)
        for batch in got["plan"]["batches"]:
            self.assertEqual(
                set(batch),
                {"selected", "operationCount", "totalGas", "estimatedCostWei"},
            )
            self.assertEqual(batch["operationCount"], "1")
            self.assertEqual(batch["totalGas"], str(ITEM_GAS))
            self.assertEqual(batch["estimatedCostWei"], str(ITEM_COST))
        self.assertEqual(got["plan"]["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(got["plan"]["estimatedCostWei"], str(ITEM_COST * 2))

    def test_unapproved_keep_original_reason(self):
        rejected = make_request(sender=SENDER_A, nonce=nonce_of(0, 0))
        rejected["userOperation"]["preVerificationGas"] = "0x2000000"  # 超代付 gas
        expensive = make_request(sender=SENDER_B, nonce=nonce_of(0, 0), max_fee=0x10)
        expensive["userOperation"]["callGasLimit"] = "0x90000"  # 成本超代付预算
        reqs = [rejected, expensive, make_request(sender=SENDER_C, nonce=0)]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    sponsorship_gas=0x100000,
                    sponsorship_budget=ITEM_COST,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [2])
        self.assertEqual(
            got["plan"]["skipped"],
            [
                {"index": 0, "reason": "E_GAS_LIMIT"},
                {"index": 1, "reason": "E_BUDGET"},
            ],
        )

    def test_nonce_chain_continues_across_batches_with_cap(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3, 6)]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    nonce_state={SENDER_A: [state_entry(0, 2)]},
                    max_batches=3,
                    max_ops=1,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0], [1], [2]],
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {SENDER_A: [{"nonceKey": "0", "lastSequence": "5"}]},
        )


class TestStateCarryForward(unittest.TestCase):
    def test_usage_and_state_carry_forward(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    usage={SENDER_A: usage_entry(100, 7)},
                    max_batches=3,
                    max_ops=2,
                    max_per_sender_per_batch=1,
                )
            )
        )
        # cap=1：A0 独占首批，A1、B2 同处第二批（批次为入选序列的连续分段）。
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0], [1, 2]],
        )
        self.assertEqual(
            got["nextSenderUsage"],
            {
                SENDER_A: {
                    "totalGas": str(100 + ITEM_GAS * 2),
                    "estimatedCostWei": str(7 + ITEM_COST * 2),
                },
                SENDER_B: {
                    "totalGas": str(ITEM_GAS),
                    "estimatedCostWei": str(ITEM_COST),
                },
            },
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {
                SENDER_A: [{"nonceKey": "0", "lastSequence": "1"}],
                SENDER_B: [{"nonceKey": "0", "lastSequence": "0"}],
            },
        )

    def test_unselected_items_not_carried(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
        ]
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batches=1,
                    max_ops=1,
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0])
        self.assertEqual(
            got["nextSenderNonceState"],
            {SENDER_A: [{"nonceKey": "0", "lastSequence": "0"}]},
        )

    def test_input_state_preserved(self):
        got = result_of(
            plan_batch_sequence_sender_fair(
                document(
                    [make_request(nonce=nonce_of(0, 0))],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={SENDER_C: usage_entry(5, 9)},
                    nonce_state={
                        SENDER_A: [state_entry(3, 8)],
                        SENDER_B: [state_entry(1, 2)],
                    },
                    max_per_sender_per_batch=1,
                )
            )
        )
        self.assertEqual(
            got["nextSenderUsage"],
            {
                SENDER_A: {
                    "totalGas": str(ITEM_GAS),
                    "estimatedCostWei": str(ITEM_COST),
                },
                SENDER_C: {"totalGas": "5", "estimatedCostWei": "9"},
            },
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {
                SENDER_A: [
                    {"nonceKey": "0", "lastSequence": "0"},
                    {"nonceKey": "3", "lastSequence": "8"},
                ],
                SENDER_B: [{"nonceKey": "1", "lastSequence": "2"}],
            },
        )


class TestNoMutation(unittest.TestCase):
    def test_input_not_mutated(self):
        doc = document(
            [
                make_request(nonce=nonce_of(0, 0)),
                make_request(nonce=nonce_of(0, 1)),
                make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
            ],
            bundle_gas=ITEM_GAS * 3,
            bundle_cost=ITEM_COST * 3,
            usage={SENDER_A: usage_entry(10, 20)},
            nonce_state={SENDER_A: [state_entry(0, 0)]},
            max_batches=2,
            max_ops=2,
            max_per_sender_per_batch=1,
        )
        snapshot = copy.deepcopy(doc)
        plan_batch_sequence_sender_fair(doc)
        self.assertEqual(doc, snapshot)


class TestRandomAgainstBruteForce(unittest.TestCase):
    """随机小实例：与暴力枚举的最优解逐一比对。"""

    def test_random_instances(self):
        rng = random.Random(20261009)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(300):
            count = rng.randint(0, 7)
            reqs = [
                make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10]),
                    sender=rng.choice(senders),
                    nonce=nonce_of(rng.randint(0, 2), rng.randint(0, 3)),
                )
                for _ in range(count)
            ]
            usage = {
                sender: usage_entry(rng.randint(0, 100), rng.randint(0, 1000))
                for sender in senders
                if rng.random() < 0.5
            }
            nonce_state = {}
            for sender in senders:
                if rng.random() < 0.6:
                    continue
                nonce_state[sender] = [
                    state_entry(key, rng.randint(0, 2))
                    for key in range(3)
                    if rng.random() < 0.4
                ]
            doc = document(
                reqs,
                bundle_gas=rng.randint(1, ITEM_GAS * 3),
                bundle_cost=rng.randint(1, ITEM_COST * 3),
                max_gas_per_sender=rng.randint(1, ITEM_GAS * 3),
                max_cost_per_sender=rng.randint(1, ITEM_COST * 3),
                usage=usage,
                nonce_state=nonce_state,
                max_batches=rng.randint(1, 4),
                max_ops=rng.randint(1, 3),
                max_per_sender_per_batch=rng.randint(1, 3),
            )
            got = plan_batch_sequence_sender_fair(doc)
            self.assertTrue(got["ok"], got)
            want_selected, want_batches = brute_force_plan(doc)
            self.assertEqual(
                got["plan"]["selected"], list(want_selected), f"doc={doc}"
            )
            self.assertEqual(
                [batch["selected"] for batch in got["plan"]["batches"]],
                want_batches,
                f"doc={doc}",
            )
            self.assertEqual(
                got["plan"]["operationCount"], str(len(want_selected))
            )
            skipped_indexes = [item["index"] for item in got["plan"]["skipped"]]
            self.assertEqual(
                sorted(skipped_indexes + got["plan"]["selected"]),
                list(range(count)),
            )
            max_spb = int(
                doc["fairnessPolicy"]["maxPerSenderPerBatch"], 16
            )
            for batch in got["plan"]["batches"]:
                self.assertLessEqual(
                    len(batch["selected"]),
                    int(doc["batchPolicy"]["maxOperationsPerBatch"], 16),
                )
                self.assertLessEqual(
                    int(batch["totalGas"]),
                    int(doc["bundlePolicy"]["maxTotalGas"], 16),
                )
                self.assertLessEqual(
                    int(batch["estimatedCostWei"]),
                    int(doc["bundlePolicy"]["maxCostWei"], 16),
                )
                batch_senders = [
                    reqs[index]["userOperation"]["sender"].lower()
                    for index in batch["selected"]
                ]
                for sender in set(batch_senders):
                    self.assertLessEqual(
                        batch_senders.count(sender), max_spb
                    )
            self.assertLessEqual(
                len(got["plan"]["batches"]),
                int(doc["batchPolicy"]["maxBatchCount"], 16),
            )

            # nextSenderUsage / nextSenderNonceState 按入选集合独立重算核对。
            selected = set(got["plan"]["selected"])
            expect_usage = {
                key.lower(): {
                    "totalGas": int(entry["totalGas"]),
                    "estimatedCostWei": int(entry["estimatedCostWei"]),
                }
                for key, entry in usage.items()
            }
            expect_state = {
                sender: {
                    int(entry["nonceKey"]): int(entry["lastSequence"])
                    for entry in entries
                }
                for sender, entries in nonce_state.items()
            }
            for index in selected:
                op = reqs[index]["userOperation"]
                sender = op["sender"].lower()
                gas = (
                    int(op["callGasLimit"], 16)
                    + int(op["verificationGasLimit"], 16)
                    + int(op["preVerificationGas"], 16)
                )
                cost = gas * int(op["maxFeePerGas"], 16)
                record = expect_usage.setdefault(
                    sender, {"totalGas": 0, "estimatedCostWei": 0}
                )
                record["totalGas"] += gas
                record["estimatedCostWei"] += cost
                nonce = int(op["nonce"], 16)
                key, sequence = divmod(nonce, NONCE_MOD)
                keys = expect_state.setdefault(sender, {})
                keys[key] = max(keys.get(key, -1), sequence)
            self.assertEqual(
                got["nextSenderUsage"],
                {
                    sender: {
                        "totalGas": str(expect_usage[sender]["totalGas"]),
                        "estimatedCostWei": str(
                            expect_usage[sender]["estimatedCostWei"]
                        ),
                    }
                    for sender in sorted(expect_usage)
                },
            )
            self.assertEqual(
                got["nextSenderNonceState"],
                {
                    sender: [
                        {"nonceKey": str(key), "lastSequence": str(keys[key])}
                        for key in sorted(keys)
                    ]
                    for sender, keys in sorted(expect_state.items())
                },
            )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.batch_sequence_sender_fair"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(nonce=nonce_of(0, i)) for i in range(3)],
            bundle_gas=ITEM_GAS * 3,
            bundle_cost=ITEM_COST * 3,
            max_batches=3,
            max_ops=3,
            max_per_sender_per_batch=1,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result), {"ok", "plan", "nextSenderUsage", "nextSenderNonceState"}
        )
        self.assertEqual(
            [batch["selected"] for batch in result["plan"]["batches"]],
            [[0], [1], [2]],
        )

    def test_fairness_policy_error_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["fairnessPolicy"] = {"maxPerSenderPerBatch": "0x0"}
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_FAIRNESS_POLICY_INVALID_FIELD)
        self.assertEqual(
            error["path"], "/fairnessPolicy/maxPerSenderPerBatch"
        )

    def test_malformed_json_exit_1_invalid_json(self):
        code, out, _ = self.run_cli("{not json")
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_INVALID_JSON)
        self.assertEqual(error["path"], "")

    def test_empty_input_exit_1_invalid_json(self):
        code, out, _ = self.run_cli("")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)

    def test_non_object_json_exit_1(self):
        code, out, _ = self.run_cli("[1, 2]")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)


if __name__ == "__main__":
    unittest.main()
