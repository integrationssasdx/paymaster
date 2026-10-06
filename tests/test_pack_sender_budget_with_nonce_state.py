"""paymaster.plan_bundle_sender_budget_with_nonce_state 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_sender_budget_with_nonce_state -v
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
    E_NONCE_STATE_INVALID_FIELD,
    E_USAGE_INVALID_FIELD,
    _choose_sender_budget_with_nonce_state,
    plan_bundle_sender_budget_with_nonce_state,
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

# nonce 拆分时 sequence 的模数（2 的 64 次方）。
SEQUENCE_MOD = 1 << 64


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
    nonce_state: dict | None = None,
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
    }


def usage_entry(gas: int, cost: int) -> dict:
    return {"totalGas": str(gas), "estimatedCostWei": str(cost)}


def state_entry(nonce_key: int, last_sequence: int) -> dict:
    return {"nonceKey": str(nonce_key), "lastSequence": str(last_sequence)}


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


class TestChooseSenderBudgetWithNonceState(unittest.TestCase):
    """直接对带 nonce 状态的选择核心做穷举交叉验证。"""

    def brute_force(
        self,
        candidates,
        usage,
        starts,
        max_gas_sender,
        max_cost_sender,
        max_gas,
        max_cost,
    ):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            gas = sum(candidates[i][3] for i in chosen)
            cost = sum(candidates[i][4] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            per_gas: dict = {}
            per_cost: dict = {}
            for i in chosen:
                sender = candidates[i][0]
                base_gas, base_cost = usage.get(sender, (0, 0))
                per_gas[sender] = per_gas.get(sender, base_gas) + candidates[i][3]
                per_cost[sender] = (
                    per_cost.get(sender, base_cost) + candidates[i][4]
                )
            if any(value > max_gas_sender for value in per_gas.values()):
                continue
            if any(value > max_cost_sender for value in per_cost.values()):
                continue
            groups: dict = {}
            for i in chosen:
                sender, key, sequence = candidates[i][:3]
                groups.setdefault((sender, key), []).append(sequence)
            feasible = True
            for group_key, sequences in groups.items():
                start = starts[group_key]
                # chosen 按候选序号递增，组内即下标递增顺序。
                if sequences != [start + offset for offset in range(len(sequences))]:
                    feasible = False
                    break
            if not feasible:
                continue
            batch_senders = {candidates[i][0] for i in chosen}
            key = (-len(chosen), -len(batch_senders), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        return best

    def test_empty(self):
        self.assertEqual(
            _choose_sender_budget_with_nonce_state(
                [], {"a": (3, 3)}, {}, 1, 1, 10, 10
            ),
            ((), 0, 0),
        )

    def test_chain_must_start_at_state_start(self):
        # 状态起点为 2：sequence 2、3 可接续，sequence 5 跳过 4 不可入。
        candidates = [("a", 0, 2, 1, 1), ("a", 0, 3, 1, 1), ("a", 0, 5, 1, 1)]
        starts = {("a", 0): 2}
        chosen, gas, cost = _choose_sender_budget_with_nonce_state(
            candidates, {}, starts, 10, 10, 10, 10
        )
        self.assertEqual((chosen, gas, cost), ((0, 1), 2, 2))

    def test_stateless_group_starts_at_zero(self):
        candidates = [("a", 0, 1, 1, 1), ("a", 0, 0, 1, 1)]
        starts = {("a", 0): 0}
        chosen, _, _ = _choose_sender_budget_with_nonce_state(
            candidates, {}, starts, 10, 10, 10, 10
        )
        # 下标递增时 sequence 必须递增：只有候选 1（sequence 0）能成链。
        self.assertEqual(chosen, (1,))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261006)
        for _ in range(2000):
            count = rng.randint(0, 8)
            raw = [
                (
                    rng.choice("ab"),
                    rng.randint(0, 1),
                    rng.randint(0, 3),
                    rng.randint(1, 5),
                    rng.randint(1, 30),
                )
                for _ in range(count)
            ]
            starts = {}
            for sender, key, *_ in raw:
                if (sender, key) not in starts:
                    starts[(sender, key)] = rng.randint(0, 2)
            candidates = [
                item for item in raw if item[2] >= starts[(item[0], item[1])]
            ]
            usage = {
                sender: (rng.randint(0, 10), rng.randint(0, 60))
                for sender in "ab"
                if rng.random() < 0.5
            }
            max_gas_sender = rng.randint(1, 15)
            max_cost_sender = rng.randint(1, 90)
            max_gas = rng.randint(0, 20)
            max_cost = rng.randint(0, 100)
            got = _choose_sender_budget_with_nonce_state(
                candidates,
                usage,
                starts,
                max_gas_sender,
                max_cost_sender,
                max_gas,
                max_cost,
            )
            want = self.brute_force(
                candidates,
                usage,
                starts,
                max_gas_sender,
                max_cost_sender,
                max_gas,
                max_cost,
            )
            self.assertEqual(
                got,
                want,
                (candidates, usage, starts, max_gas_sender, max_cost_sender,
                 max_gas, max_cost),
            )


class TestPlanEndToEnd(unittest.TestCase):
    def test_empty_state_matches_usage_baseline(self):
        doc = document(
            [make_request(), make_request(sender=SENDER_B)],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )
        plan = plan_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))

    def test_empty_requests_and_empty_state_succeeds(self):
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document([], bundle_gas=1, bundle_cost=1)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_stateless_chain_from_zero(self):
        reqs = [make_request(nonce=0), make_request(nonce=1), make_request(nonce=2)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_gas_per_sender=ITEM_GAS * 3,
                    max_cost_per_sender=ITEM_COST * 3,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])

    def test_stateless_gap_skipped(self):
        # 缺 sequence 1：sequence 2 不能从 0 连续接续。
        reqs = [make_request(nonce=0), make_request(nonce=2)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_stateful_chain_continues_from_last_sequence(self):
        reqs = [make_request(nonce=5), make_request(nonce=6)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])

    def test_sequence_not_above_last_sequence_is_conflict(self):
        reqs = [make_request(nonce=3), make_request(nonce=4), make_request(nonce=5)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [2])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_NONCE_CONFLICT"},
                {"index": 1, "reason": "E_NONCE_CONFLICT"},
            ],
        )

    def test_stateful_gap_when_not_starting_at_last_plus_one(self):
        # 状态 lastSequence 为 4，只有 sequence 6：无法从 5 接续。
        reqs = [make_request(nonce=6)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_duplicate_sequence_is_conflict(self):
        reqs = [make_request(nonce=0), make_request(nonce=0)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_index_order_must_match_sequence_order(self):
        # sequence 1 在前、sequence 0 在后：只有后者能成链。
        reqs = [make_request(nonce=1), make_request(nonce=0)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_nonce_keys_are_independent(self):
        # key 0 有状态（从 5 开始），key 1 无状态（从 0 开始）。
        reqs = [
            make_request(nonce=5),
            make_request(nonce=SEQUENCE_MOD),
            make_request(nonce=SEQUENCE_MOD + 1),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_gas_per_sender=ITEM_GAS * 3,
                    max_cost_per_sender=ITEM_COST * 3,
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])

    def test_senders_are_independent(self):
        reqs = [
            make_request(sender=SENDER_A, nonce=5),
            make_request(sender=SENDER_B, nonce=0),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [])

    def test_usage_consumes_sender_budget_with_chain(self):
        reqs = [make_request(nonce=5), make_request(nonce=6)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                    usage={SENDER_A: usage_entry(ITEM_GAS, 0)},
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_GAS"}])

    def test_unapproved_request_keeps_reason_and_no_allowance(self):
        rejected = make_request(nonce=0)
        rejected["userOperation"]["preVerificationGas"] = "0x200000"
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    [rejected, make_request(nonce=0, sender=SENDER_B)],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                )
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])

    def test_bundle_limits_still_apply(self):
        reqs = [make_request(nonce=0), make_request(nonce=1)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0])
        # sequence 1 可接续，但 bundle gas 已满。
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_reason_order_nonce_before_sender_budget(self):
        # sequence 3 不能接续（起点 0 且组内入选到 1），同时 sender 额度已满：
        # 断链先于 sender 限额。
        reqs = [make_request(nonce=0), make_request(nonce=1), make_request(nonce=3)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_NONCE_GAP"}])

    def test_distinct_senders_still_preferred(self):
        # 容量恰够两项：{0,1} 同 sender 链，{0,2} 含两个 sender。
        reqs = [
            make_request(sender=SENDER_A, nonce=0),
            make_request(sender=SENDER_A, nonce=1),
            make_request(sender=SENDER_B, nonce=0),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_last_sequence_max_blocks_group(self):
        # lastSequence 为 2**64 - 1：任何 sequence 都不高于它，全部冲突。
        reqs = [make_request(nonce=0), make_request(nonce=SEQUENCE_MOD - 1)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    nonce_state={SENDER_A: [state_entry(0, SEQUENCE_MOD - 1)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_NONCE_CONFLICT"},
                {"index": 1, "reason": "E_NONCE_CONFLICT"},
            ],
        )

    def test_inputs_not_mutated(self):
        doc = document(
            [make_request(nonce=5), make_request(nonce=6)],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
            usage={SENDER_A: usage_entry(1, 2)},
            nonce_state={SENDER_A: [state_entry(0, 4)]},
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_sender_budget_with_nonce_state(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_violates_first_reason_in_order(self):
        """未入选的 approved 项必触发某条约束，原因按 nonce 冲突、断链、
        sender gas、sender cost、bundle gas、bundle budget 的次序取首个。"""
        rng = random.Random(29)
        senders = [SENDER_A, SENDER_B]
        for _ in range(80):
            count = rng.randint(1, 6)
            reqs = [
                make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10]),
                    sender=rng.choice(senders),
                    nonce=rng.randint(0, 3),
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
            nonce_state = {}
            for sender in senders:
                if rng.random() < 0.5:
                    nonce_state[sender] = [state_entry(0, rng.randint(0, 2))]
            doc = document(
                reqs,
                bundle_gas=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                bundle_cost=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
                max_gas_per_sender=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                max_cost_per_sender=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
                usage=usage,
                nonce_state=nonce_state,
            )
            plan = plan_of(plan_bundle_sender_budget_with_nonce_state(doc))
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
            # 入选项本身满足全部约束。
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
            group_sequences: dict = {}
            for index in sorted(selected):
                op = reqs[index]["userOperation"]
                sender = op["sender"].lower()
                nonce = int(op["nonce"], 16)
                key, sequence = divmod(nonce, SEQUENCE_MOD)
                gas, cost = profiles[index]
                per_sender_gas[sender] = per_sender_gas.get(sender, 0) + gas
                per_sender_cost[sender] = per_sender_cost.get(sender, 0) + cost
                self.assertLessEqual(per_sender_gas[sender], max_gas_sender)
                self.assertLessEqual(per_sender_cost[sender], max_cost_sender)
                chain = group_sequences.setdefault((sender, key), [])
                state = {
                    s.lower(): {int(e["nonceKey"]): int(e["lastSequence"]) for e in entries}
                    for s, entries in nonce_state.items()
                }
                last = state.get(sender, {}).get(key)
                start = last + 1 if last is not None else 0
                self.assertEqual(sequence, start + len(chain))
                chain.append(sequence)
            # 未入选项的原因与次序一致。
            skipped_reasons = {
                item["index"]: item["reason"] for item in plan["skipped"]
            }
            for index, request in enumerate(reqs):
                if index in selected:
                    continue
                reason = skipped_reasons[index]
                if reason in ("E_GAS_LIMIT", "E_BUDGET"):
                    continue
                op = request["userOperation"]
                sender = op["sender"].lower()
                nonce = int(op["nonce"], 16)
                key, sequence = divmod(nonce, SEQUENCE_MOD)
                gas, cost = profiles[index]
                state = {
                    s.lower(): {int(e["nonceKey"]): int(e["lastSequence"]) for e in entries}
                    for s, entries in nonce_state.items()
                }
                last = state.get(sender, {}).get(key)
                start = last + 1 if last is not None else 0
                chain = group_sequences.get((sender, key), [])
                conflict = (last is not None and sequence <= last) or (
                    sequence in chain
                )
                extends = (
                    not conflict
                    and sequence >= start
                    and sequence == start + len(chain)
                )
                if extends and chain:
                    # 下标须在链尾之后；chain 只存 sequence，此处用候选顺序近似
                    # 检查：若该下标小于某个入选同组项的下标则不可接续。
                    later = [
                        i
                        for i in selected
                        if i > index
                        and reqs[i]["userOperation"]["sender"].lower() == sender
                        and divmod(
                            int(reqs[i]["userOperation"]["nonce"], 16),
                            SEQUENCE_MOD,
                        )[0]
                        == key
                    ]
                    if later:
                        extends = False
                over_sender_gas = (
                    per_sender_gas.get(sender, 0) + gas > max_gas_sender
                )
                over_sender_cost = (
                    per_sender_cost.get(sender, 0) + cost > max_cost_sender
                )
                over_gas = total_gas + gas > max_gas
                if conflict:
                    expected = "E_NONCE_CONFLICT"
                elif not extends:
                    expected = "E_NONCE_GAP"
                elif over_sender_gas:
                    expected = "E_SENDER_GAS"
                elif over_sender_cost:
                    expected = "E_SENDER_COST"
                elif over_gas:
                    expected = "E_BUNDLE_GAS"
                else:
                    expected = "E_BUNDLE_BUDGET"
                self.assertEqual(reason, expected, (doc, plan, index))


class TestSenderNonceStateValidation(unittest.TestCase):
    """senderNonceState 的结构与字段校验：一律 E_NONCE_STATE_INVALID_FIELD。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def assert_state_error(self, doc, path):
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(err["code"], E_NONCE_STATE_INVALID_FIELD)
        self.assertEqual(err["path"], path)

    def test_state_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(state=bad):
                doc = self.valid_doc()
                doc["senderNonceState"] = bad
                self.assert_state_error(doc, "/senderNonceState")

    def test_empty_state_object_ok(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {}
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_invalid_address_keys(self):
        for bad_key in (
            "0x1234",
            "0x" + "1" * 41,
            "1111111111111111111111111111111111111111",
            "0x" + "g" * 40,
            "0x" + "0" * 40,  # 零地址
            "0X" + "1" * 40,
            "0x" + "AB" * 20,  # 大写非规范形式
            "0x" + "aB" * 20,  # 混合大小写非规范形式
            "",
        ):
            with self.subTest(key=bad_key):
                doc = self.valid_doc()
                doc["senderNonceState"] = {bad_key: []}
                self.assert_state_error(doc, "/senderNonceState/" + bad_key)

    def test_value_not_array(self):
        for bad in ({}, "x", 1, None, True):
            with self.subTest(value=bad):
                doc = self.valid_doc()
                doc["senderNonceState"] = {SENDER_A: bad}
                self.assert_state_error(doc, "/senderNonceState/" + SENDER_A)

    def test_empty_array_ok(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {SENDER_A: []}
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_entry_not_object(self):
        for bad in ([], "x", 1, None):
            with self.subTest(entry=bad):
                doc = self.valid_doc()
                doc["senderNonceState"] = {SENDER_A: [bad]}
                self.assert_state_error(doc, "/senderNonceState/" + SENDER_A + "/0")

    def test_entry_missing_fields(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {SENDER_A: [{"lastSequence": "0"}]}
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/nonceKey"
        )

        doc = self.valid_doc()
        doc["senderNonceState"] = {SENDER_A: [{"nonceKey": "0"}]}
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/lastSequence"
        )

    def test_entry_unknown_field(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{**state_entry(0, 0), "extra": "0"}]
        }
        self.assert_state_error(doc, "/senderNonceState/" + SENDER_A + "/0/extra")

    def test_entry_missing_checked_before_unknown(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {SENDER_A: [{"extra": "0"}]}
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/nonceKey"
        )

    def test_invalid_decimal_values(self):
        for bad in ("01", "-1", "0x10", "", " 1", "1 ", "1.0", "+1", 1, 0, None, True, [], {}):
            with self.subTest(value=bad):
                doc = self.valid_doc()
                doc["senderNonceState"] = {
                    SENDER_A: [{"nonceKey": bad, "lastSequence": "0"}]
                }
                self.assert_state_error(
                    doc, "/senderNonceState/" + SENDER_A + "/0/nonceKey"
                )

    def test_invalid_last_sequence_value(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": "0", "lastSequence": "007"}]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/lastSequence"
        )

    def test_values_must_be_less_than_2_to_the_64(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(SEQUENCE_MOD, 0)]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/nonceKey"
        )

        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(0, SEQUENCE_MOD)]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/lastSequence"
        )

        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(SEQUENCE_MOD - 1, SEQUENCE_MOD - 1)]
        }
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_duplicate_nonce_key_same_sender(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(1, 0), state_entry(1, 2)]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/1/nonceKey"
        )

    def test_same_nonce_key_different_senders_ok(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(1, 0)],
            SENDER_B: [state_entry(1, 2)],
        }
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_multiple_entries_per_sender_ok(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(0, 4), state_entry(7, 9)],
        }
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_state_checked_after_sender_usage(self):
        doc = self.valid_doc()
        doc["senderUsage"] = []
        doc["senderNonceState"] = []
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(
            (err["code"], err["path"]), (E_USAGE_INVALID_FIELD, "/senderUsage")
        )

        doc = self.valid_doc()
        doc["senderBudgetPolicy"] = {}
        doc["senderNonceState"] = {"bad-key": []}
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_MISSING_FIELD, "/senderBudgetPolicy/maxTotalGasPerSender"),
        )


class TestStructureErrorsParity(unittest.TestCase):
    """根结构错误码、path 与检查顺序与 plan_bundle_sender_budget_with_usage 一致。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle_sender_budget_with_nonce_state(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(
            plan_bundle_sender_budget_with_nonce_state(
                {"requests": [], "extra": 1}
            )
        )
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_sender_budget_with_nonce_state({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_sender_budget_with_nonce_state({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_sender_budget_with_nonce_state(
                {"requests": [], "sponsorshipPolicy": {}}
            )
        )
        self.assertEqual(err["path"], "/bundlePolicy")
        err = error_of(
            plan_bundle_sender_budget_with_nonce_state(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                }
            )
        )
        self.assertEqual(err["path"], "/senderBudgetPolicy")
        err = error_of(
            plan_bundle_sender_budget_with_nonce_state(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                    "senderBudgetPolicy": {},
                }
            )
        )
        self.assertEqual(err["path"], "/senderUsage")
        err = error_of(
            plan_bundle_sender_budget_with_nonce_state(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                    "senderBudgetPolicy": {},
                    "senderUsage": {},
                }
            )
        )
        self.assertEqual(err["path"], "/senderNonceState")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
                self.assertEqual(
                    (err["code"], err["path"]),
                    (E_INVALID_FIELD, "/requests"),
                )

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_MISSING_FIELD, "/requests/1/userOperation/signature"),
        )

    def test_policy_errors_unchanged(self):
        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"),
        )

        doc = self.valid_doc()
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
        self.assertEqual(
            (err["code"], err["path"]),
            (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxTotalGas"),
        )

    def test_result_shapes(self):
        result = plan_bundle_sender_budget_with_nonce_state({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_sender_budget_with_nonce_state(
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
            paymaster.plan_bundle_sender_budget_with_nonce_state,
            plan_bundle_sender_budget_with_nonce_state,
        )
        self.assertIn(
            "plan_bundle_sender_budget_with_nonce_state", paymaster.__all__
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_sender_budget_with_nonce_state"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(nonce=5), make_request(nonce=6)],
            bundle_gas=ITEM_GAS * 2,
            bundle_cost=ITEM_COST * 2,
            nonce_state={SENDER_A: [state_entry(0, 4)]},
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1])

    def test_nonce_conflict_exit_0(self):
        doc = document(
            [make_request(nonce=4)],
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
            nonce_state={SENDER_A: [state_entry(0, 4)]},
        )
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["plan"]["skipped"],
            [{"index": 0, "reason": "E_NONCE_CONFLICT"}],
        )

    def test_nonce_state_error_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": "01", "lastSequence": "0"}]
        }
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_NONCE_STATE_INVALID_FIELD)
        self.assertEqual(
            error["path"], "/senderNonceState/" + SENDER_A + "/0/nonceKey"
        )

    def test_missing_sender_nonce_state_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["senderNonceState"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/senderNonceState")

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
