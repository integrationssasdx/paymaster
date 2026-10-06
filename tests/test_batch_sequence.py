"""paymaster.plan_batch_sequence 与其 CLI 的单元测试。

运行：python -m unittest tests.test_batch_sequence -v
"""

from __future__ import annotations

import copy
import itertools
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import paymaster  # noqa: E402
from paymaster.batch_sequence import (  # noqa: E402
    E_BATCH_POLICY_INVALID_FIELD,
    E_BATCH_POLICY_MISSING_FIELD,
    E_BATCH_POLICY_UNKNOWN_FIELD,
    plan_batch_sequence,
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
    }


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
    """暴力枚举所有入选子集，返回 (selected, batches) 的最优解。"""
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
        # sender 聚合限额（含既有用量）。
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
        # 贪心最长前缀划分批次。
        batches = []
        current = []
        fill_count = fill_gas = fill_cost = 0
        for item in chosen:
            gas, cost = item[4], item[5]
            if current and (
                fill_count + 1 > max_ops
                or fill_gas + gas > max_total_gas
                or fill_cost + cost > max_cost_wei
            ):
                batches.append(current)
                current = []
                fill_count = fill_gas = fill_cost = 0
            current.append(item)
            fill_count += 1
            fill_gas += gas
            fill_cost += cost
        if current:
            batches.append(current)
        # 单项即超批限额时该子集不可行；其余情形贪心划分必满足限额。
        for batch in batches:
            if (
                len(batch) > max_ops
                or sum(item[4] for item in batch) > max_total_gas
                or sum(item[5] for item in batch) > max_cost_wei
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
    """文档结构与 batchPolicy 校验。"""

    def valid_doc(self):
        return document(
            [make_request()], bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                error = error_of(plan_batch_sequence(bad))
                self.assertEqual(error["code"], E_INVALID_JSON)
                self.assertEqual(error["path"], "")

    def test_unknown_root_field(self):
        doc = self.valid_doc()
        doc["extra"] = 1
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_UNKNOWN_FIELD)
        self.assertEqual(error["path"], "/extra")

    def test_missing_root_fields(self):
        error = error_of(plan_batch_sequence({}))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/requests")
        doc = self.valid_doc()
        del doc["batchPolicy"]
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/batchPolicy")

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        del doc["requests"][0]["userOperation"]["signature"]
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertTrue(error["path"].startswith("/requests/0/"))

    def test_batch_policy_not_object(self):
        doc = self.valid_doc()
        doc["batchPolicy"] = []
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_INVALID_FIELD)
        self.assertEqual(error["path"], "/batchPolicy")

    def test_batch_policy_missing_fields(self):
        doc = self.valid_doc()
        doc["batchPolicy"] = {"maxOperationsPerBatch": "0x2"}
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/maxBatchCount")
        doc["batchPolicy"] = {"maxBatchCount": "0x2"}
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/maxOperationsPerBatch")

    def test_batch_policy_unknown_field(self):
        doc = self.valid_doc()
        doc["batchPolicy"]["extra"] = "0x1"
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_UNKNOWN_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/extra")

    def test_batch_policy_invalid_values(self):
        for bad in ("0x0", "0x01", "01", "1", "", 1, None, True, "0x"):
            doc = self.valid_doc()
            doc["batchPolicy"]["maxBatchCount"] = bad
            with self.subTest(value=bad):
                error = error_of(plan_batch_sequence(doc))
                self.assertEqual(error["code"], E_BATCH_POLICY_INVALID_FIELD)
                self.assertEqual(error["path"], "/batchPolicy/maxBatchCount")

    def test_batch_policy_upper_bound(self):
        doc = self.valid_doc()
        doc["batchPolicy"]["maxOperationsPerBatch"] = hex((1 << 64) - 1)
        self.assertTrue(plan_batch_sequence(doc)["ok"])
        doc["batchPolicy"]["maxOperationsPerBatch"] = hex(1 << 64)
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_BATCH_POLICY_INVALID_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/maxOperationsPerBatch")

    def test_batch_policy_checked_last(self):
        doc = self.valid_doc()
        doc["senderUsage"] = []
        doc["batchPolicy"] = []
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(error["path"], "/senderUsage")
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": "01", "lastSequence": "0"}]
        }
        doc["batchPolicy"] = []
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(error["code"], E_NONCE_STATE_INVALID_FIELD)

    def test_error_result_shape(self):
        doc = self.valid_doc()
        doc["batchPolicy"] = {}
        error = error_of(plan_batch_sequence(doc))
        self.assertEqual(set(error), {"code", "path", "message"})

    def test_exported_from_package(self):
        self.assertIs(paymaster.plan_batch_sequence, plan_batch_sequence)
        self.assertIn("plan_batch_sequence", paymaster.__all__)


class TestPlanning(unittest.TestCase):
    """分批、约束与目标优先级。"""

    def test_empty_requests(self):
        got = result_of(
            plan_batch_sequence(document([], bundle_gas=1, bundle_cost=1))
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

    def test_single_batch_when_everything_fits(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3)]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_ops=3,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(len(got["plan"]["batches"]), 1)
        self.assertEqual(got["plan"]["batches"][0]["selected"], [0, 1, 2])
        self.assertEqual(got["plan"]["batches"][0]["operationCount"], "3")
        self.assertEqual(got["plan"]["batches"][0]["totalGas"], str(ITEM_GAS * 3))
        self.assertEqual(
            got["plan"]["batches"][0]["estimatedCostWei"], str(ITEM_COST * 3)
        )

    def test_split_by_max_operations_per_batch(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3)]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 4,
                    bundle_cost=ITEM_COST * 4,
                    max_ops=2,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0, 1], [2]],
        )
        self.assertEqual(got["plan"]["operationCount"], "3")
        self.assertEqual(got["plan"]["totalGas"], str(ITEM_GAS * 3))

    def test_split_by_bundle_gas_limit(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3)]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 3,
                    max_ops=3,
                )
            )
        )
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]],
            [[0, 1], [2]],
        )

    def test_max_batch_count_limits_selection(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(4)]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batches=2,
                    max_ops=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]], [[0], [1]]
        )
        self.assertEqual(
            got["plan"]["skipped"],
            [
                {"index": 2, "reason": "E_NOT_SELECTED"},
                {"index": 3, "reason": "E_NOT_SELECTED"},
            ],
        )

    def test_fewer_batches_beat_less_gas(self):
        # {0,1} 可装入 1 批但总 gas 较高；{1,2} 须 2 批但总 gas 较低：批次数
        # 目标先于 gas。{0,2} 与 {0,1,2} 因 SENDER_A 的 nonce 链断档不可行。
        cheap = make_request(sender=SENDER_A, nonce=nonce_of(0, 2))
        cheap["userOperation"]["callGasLimit"] = "0x8000"
        cheap["userOperation"]["verificationGasLimit"] = "0x8000"
        cheap["userOperation"]["preVerificationGas"] = "0x8000"
        cheap["userOperation"]["maxFeePerGas"] = "0x100"
        cheap["userOperation"]["maxPriorityFeePerGas"] = "0x100"
        # cheap：gas = 0x18000，cost = 0x18000 * 0x100 = 4 * ITEM_COST。
        reqs = [
            make_request(sender=SENDER_A, nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
            cheap,
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 4,
                    max_batches=3,
                    max_ops=2,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])
        self.assertEqual(
            [batch["selected"] for batch in got["plan"]["batches"]], [[0, 1]]
        )

    def test_more_senders_after_batch_count(self):
        # {0,1}（同一 sender 的链）与 {0,2}（两个 sender）同为 2 项 1 批。
        reqs = [
            make_request(sender=SENDER_A, nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_A, nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_batches=1,
                    max_ops=2,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 2])

    def test_lexicographic_tie_break(self):
        reqs = [
            make_request(sender=SENDER_A, nonce=0),
            make_request(sender=SENDER_B, nonce=0),
            make_request(sender=SENDER_C, nonce=0),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_batches=1,
                    max_ops=2,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])

    def test_nonce_chain_continues_across_batches(self):
        reqs = [make_request(nonce=nonce_of(0, i)) for i in range(3, 6)]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    nonce_state={SENDER_A: [state_entry(0, 2)]},
                    max_batches=3,
                    max_ops=1,
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

    def test_nonce_gap_not_selected(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 2)),  # 缺 sequence 1，不能成链
        ]
        got = result_of(
            plan_batch_sequence(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(got["plan"]["selected"], [0])
        self.assertEqual(
            got["plan"]["skipped"], [{"index": 1, "reason": "E_NOT_SELECTED"}]
        )

    def test_sender_budget_shared_across_batches(self):
        # 既有用量使 SENDER_A 只剩一项的 gas 余量：两项分属不同批也不行。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_gas_per_sender=ITEM_GAS + 1,
                    usage={SENDER_A: usage_entry(1, 0)},
                    max_batches=3,
                    max_ops=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 2])
        self.assertEqual(
            got["plan"]["skipped"], [{"index": 1, "reason": "E_NOT_SELECTED"}]
        )

    def test_unapproved_keep_original_reason(self):
        rejected = make_request(sender=SENDER_A, nonce=nonce_of(0, 0))
        rejected["userOperation"]["preVerificationGas"] = "0x2000000"  # 超代付 gas
        expensive = make_request(sender=SENDER_B, nonce=nonce_of(0, 0), max_fee=0x10)
        expensive["userOperation"]["callGasLimit"] = "0x90000"  # 成本超代付预算
        reqs = [rejected, expensive, make_request(sender=SENDER_C, nonce=0)]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    sponsorship_gas=0x100000,
                    sponsorship_budget=ITEM_COST,
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

    def test_batch_summary_fields(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batches=2,
                    max_ops=1,
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


class TestStateCarryForward(unittest.TestCase):
    def test_usage_accumulates_all_selected(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={SENDER_A: usage_entry(100, 7)},
                    max_batches=3,
                    max_ops=1,
                )
            )
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

    def test_unselected_items_not_carried(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batches=1,
                    max_ops=1,
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
            plan_batch_sequence(
                document(
                    [make_request(nonce=nonce_of(0, 0))],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={SENDER_C: usage_entry(5, 9)},
                    nonce_state={
                        SENDER_A: [state_entry(3, 8)],
                        SENDER_B: [state_entry(1, 2)],
                    },
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
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
            usage={SENDER_A: usage_entry(10, 20)},
            nonce_state={SENDER_A: [state_entry(0, 0)]},
            max_batches=2,
            max_ops=2,
        )
        snapshot = copy.deepcopy(doc)
        plan_batch_sequence(doc)
        self.assertEqual(doc, snapshot)


class TestRandomAgainstBruteForce(unittest.TestCase):
    """随机小实例：与暴力枚举的最优解逐一比对。"""

    def test_random_instances(self):
        rng = random.Random(20261008)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(200):
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
            )
            got = plan_batch_sequence(doc)
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
            # 汇总字段与 skipped 覆盖每个请求。
            self.assertEqual(
                got["plan"]["operationCount"], str(len(want_selected))
            )
            skipped_indexes = [item["index"] for item in got["plan"]["skipped"]]
            self.assertEqual(
                sorted(skipped_indexes + got["plan"]["selected"]),
                list(range(count)),
            )
            for batch in got["plan"]["batches"]:
                self.assertLessEqual(len(batch["selected"]), int(doc["batchPolicy"]["maxOperationsPerBatch"], 16))
                self.assertLessEqual(
                    int(batch["totalGas"]), int(doc["bundlePolicy"]["maxTotalGas"], 16)
                )
                self.assertLessEqual(
                    int(batch["estimatedCostWei"]),
                    int(doc["bundlePolicy"]["maxCostWei"], 16),
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
            [sys.executable, "-m", "paymaster.batch_sequence"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(nonce=nonce_of(0, i)) for i in range(3)],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
            max_ops=2,
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
            [[0, 1], [2]],
        )

    def test_batch_policy_error_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["batchPolicy"] = {"maxBatchCount": "0x1"}
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/maxOperationsPerBatch")

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
