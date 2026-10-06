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
    """直接对带累计用量与 nonce 状态的选择核心做穷举交叉验证。"""

    def brute_force(
        self,
        candidates,
        usage,
        nonce_state,
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
                if sender not in per_gas:
                    per_gas[sender], per_cost[sender] = usage.get(sender, (0, 0))
                per_gas[sender] += candidates[i][3]
                per_cost[sender] += candidates[i][4]
            if any(value > max_gas_sender for value in per_gas.values()):
                continue
            if any(value > max_cost_sender for value in per_cost.values()):
                continue
            # 每个 (sender, key) 组的入选项须构成从起点开始的连续链。
            groups: dict = {}
            for i in chosen:
                sender, key, sequence = candidates[i][:3]
                groups.setdefault((sender, key), []).append(sequence)
            feasible = True
            for (sender, key), sequences in groups.items():
                state = nonce_state.get(sender, {}).get(key)
                start = state + 1 if state is not None else 0
                # chosen 下标递增，sequence 须依次为 start, start+1, ...
                for offset, sequence in enumerate(sequences):
                    if sequence != start + offset:
                        feasible = False
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
                [], {"a": (3, 3)}, {"a": {0: 4}}, 1, 1, 10, 10
            ),
            ((), 0, 0),
        )

    def test_stateless_group_starts_at_zero(self):
        # 无状态：sequence 1 不能单独成链，只有 sequence 0 可选。
        candidates = [("a", 0, 1, 2, 2), ("a", 0, 0, 2, 2)]
        chosen, gas, cost = _choose_sender_budget_with_nonce_state(
            candidates, {}, {}, 10, 10, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((1,), 2, 2))

    def test_stateful_group_starts_after_last_sequence(self):
        # 有状态 lastSequence=2：链从 3 开始，sequence 2 与 0 不可选。
        candidates = [
            ("a", 0, 2, 1, 1),
            ("a", 0, 3, 2, 2),
            ("a", 0, 4, 2, 2),
        ]
        chosen, gas, cost = _choose_sender_budget_with_nonce_state(
            candidates, {}, {"a": {0: 2}}, 10, 10, 100, 100
        )
        self.assertEqual((chosen, gas, cost), ((1, 2), 4, 4))

    def test_gap_breaks_chain(self):
        # sequence 0 与 2 之间缺 1：只能选 sequence 0。
        candidates = [("a", 0, 0, 2, 2), ("a", 0, 2, 2, 2)]
        chosen, _, _ = _choose_sender_budget_with_nonce_state(
            candidates, {}, {}, 10, 10, 100, 100
        )
        self.assertEqual(chosen, (0,))

    def test_usage_consumes_sender_allowance(self):
        candidates = [("a", 0, 0, 2, 2), ("a", 0, 1, 2, 2)]
        chosen, _, _ = _choose_sender_budget_with_nonce_state(
            candidates, {"a": (2, 1)}, {}, 4, 4, 100, 100
        )
        self.assertEqual(chosen, (0,))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261006)
        for _ in range(3000):
            count = rng.randint(0, 9)
            candidates = [
                (
                    rng.choice("ab"),
                    rng.randint(0, 2),
                    rng.randint(0, 6),
                    rng.randint(1, 7),
                    rng.randint(1, 40),
                )
                for _ in range(count)
            ]
            usage = {
                sender: (rng.randint(0, 15), rng.randint(0, 90))
                for sender in "ab"
                if rng.random() < 0.5
            }
            nonce_state = {}
            for sender in "ab":
                if rng.random() < 0.5:
                    continue
                nonce_state[sender] = {
                    key: rng.randint(0, 5)
                    for key in range(3)
                    if rng.random() < 0.5
                }
            max_gas_sender = rng.randint(1, 20)
            max_cost_sender = rng.randint(1, 120)
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_sender_budget_with_nonce_state(
                candidates,
                usage,
                nonce_state,
                max_gas_sender,
                max_cost_sender,
                max_gas,
                max_cost,
            )
            want = self.brute_force(
                candidates,
                usage,
                nonce_state,
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
                    nonce_state,
                    max_gas_sender,
                    max_cost_sender,
                    max_gas,
                    max_cost,
                ),
            )


class TestPlanEndToEnd(unittest.TestCase):
    def test_empty_state_matches_with_usage_baseline(self):
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

    def test_stateful_chain_continues_from_last_sequence(self):
        reqs = [
            make_request(nonce=nonce_of(0, 3)),
            make_request(nonce=nonce_of(0, 4)),
            make_request(nonce=nonce_of(0, 6)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                    nonce_state={SENDER_A: [state_entry(0, 2)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_NONCE_GAP"}])

    def test_sequence_not_above_last_sequence_conflicts(self):
        reqs = [
            make_request(nonce=nonce_of(0, 2)),
            make_request(nonce=nonce_of(0, 3)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                    nonce_state={SENDER_A: [state_entry(0, 2)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(
            plan["skipped"], [{"index": 0, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_stateless_group_must_start_at_zero(self):
        # 无状态组的链从 0 开始：sequence 1 在下标 0、sequence 0 在下标 1，
        # 只有 sequence 0 能入选；sequence 1 接上链尾但下标在链尾之前，记断链。
        reqs = [
            make_request(nonce=nonce_of(1, 1)),
            make_request(nonce=nonce_of(1, 0)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_duplicate_sequence_conflicts(self):
        reqs = [make_request(nonce=nonce_of(0, 0)) for _ in range(2)]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_max_last_sequence_blocks_whole_group(self):
        # lastSequence 为 2**64 - 1 时任何 sequence 都不高于它的反面不存在：
        # 全部不高于 lastSequence，记冲突。
        reqs = [make_request(nonce=nonce_of(0, 0))]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    nonce_state={SENDER_A: [state_entry(0, NONCE_MOD - 1)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(
            plan["skipped"], [{"index": 0, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_huge_sequence_gap(self):
        reqs = [make_request(nonce=nonce_of(0, 1 << 63))]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_keys_of_same_sender_are_independent(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(1, 0)),
            make_request(nonce=nonce_of(0, 1)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                    nonce_state={SENDER_A: [state_entry(1, 0)]},
                )
            )
        )
        # key 1 有状态 lastSequence=0：sequence 0 不高于它，记冲突；key 0 的
        # 无状态链 0、1 正常入选。
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_NONCE_CONFLICT"}]
        )

    def test_nonce_state_of_other_sender_does_not_count(self):
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    [make_request(nonce=nonce_of(0, 0))],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    nonce_state={SENDER_B: [state_entry(0, 100)]},
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [])

    def test_usage_consumes_sender_gas_budget(self):
        reqs = [make_request(nonce=nonce_of(0, 0)), make_request(nonce=nonce_of(0, 1))]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
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
        reqs = [make_request(nonce=nonce_of(0, 0)), make_request(nonce=nonce_of(0, 1))]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
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

    def test_bundle_gas_and_budget_reasons(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_BUDGET"}]
        )

    def test_gap_checked_before_sender_limits(self):
        # 断链与 sender 限额同时成立时记断链。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 2)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS,
                    max_cost_per_sender=ITEM_COST,
                )
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_unapproved_request_does_not_consume_allowance(self):
        rejected = make_request(nonce=nonce_of(0, 0))
        rejected["userOperation"]["preVerificationGas"] = "0x200000"
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    [rejected, make_request(sender=SENDER_B, nonce=nonce_of(0, 0))],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_gas_per_sender=ITEM_GAS,
                    max_cost_per_sender=ITEM_COST,
                )
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])

    def test_distinct_senders_still_preferred(self):
        # 容量恰够两项：{0,1} 同 sender，{0,2} 含两个 sender。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        plan = plan_of(
            plan_bundle_sender_budget_with_nonce_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
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
            nonce_state={SENDER_A: [state_entry(0, 3)]},
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_sender_budget_with_nonce_state(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_gets_first_reason_in_order(self):
        """未入选的 approved 项按冲突、断链、sender gas、sender cost、bundle
        gas、bundle budget 的次序取首个成立的原因。"""
        rng = random.Random(29)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(80):
            count = rng.randint(1, 6)
            reqs = [
                make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000, 0x20000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10, 0x40]),
                    sender=rng.choice(senders),
                    nonce=nonce_of(rng.randint(0, 1), rng.randint(0, 4)),
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
                    continue
                nonce_state[sender] = [
                    state_entry(key, rng.randint(0, 3))
                    for key in range(2)
                    if rng.random() < 0.5
                ]
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
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)

            def split(index):
                op = reqs[index]["userOperation"]
                nonce = int(op["nonce"], 16)
                return op["sender"].lower(), nonce // NONCE_MOD, nonce % NONCE_MOD

            # 入选项自身必须满足全部约束。
            per_sender_gas = {
                sender.lower(): int(entry["totalGas"])
                for sender, entry in usage.items()
            }
            per_sender_cost = {
                sender.lower(): int(entry["estimatedCostWei"])
                for sender, entry in usage.items()
            }
            chosen_sequences: dict = {}
            for index in sorted(selected):
                sender, key, sequence = split(index)
                gas, cost = profiles[index]
                per_sender_gas[sender] = per_sender_gas.get(sender, 0) + gas
                per_sender_cost[sender] = per_sender_cost.get(sender, 0) + cost
                self.assertLessEqual(per_sender_gas[sender], max_gas_sender)
                self.assertLessEqual(per_sender_cost[sender], max_cost_sender)
                chosen_sequences.setdefault((sender, key), {})[sequence] = index
            for (sender, key), sequences in chosen_sequences.items():
                state = nonce_state.get(sender, [])
                last = next(
                    (int(e["lastSequence"]) for e in state
                     if int(e["nonceKey"]) == key),
                    None,
                )
                start = last + 1 if last is not None else 0
                ordered = sorted(sequences.items(), key=lambda item: item[1])
                self.assertEqual(
                    [sequence for sequence, _ in ordered],
                    [start + offset for offset in range(len(ordered))],
                )

            skipped_reasons = {
                item["index"]: item["reason"] for item in plan["skipped"]
            }
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                sender, key, sequence = split(index)
                state = nonce_state.get(sender, [])
                last = next(
                    (int(e["lastSequence"]) for e in state
                     if int(e["nonceKey"]) == key),
                    None,
                )
                sequences = chosen_sequences.get((sender, key), {})
                if sequence in sequences or (
                    last is not None and sequence <= last
                ):
                    expected = "E_NONCE_CONFLICT"
                else:
                    start = last + 1 if last is not None else 0
                    if not sequences:
                        extends = sequence == start
                    else:
                        highest = max(sequences)
                        extends = (
                            sequence == highest + 1
                            and index > sequences[highest]
                        )
                    if not extends:
                        expected = "E_NONCE_GAP"
                    elif per_sender_gas.get(sender, 0) + gas > max_gas_sender:
                        expected = "E_SENDER_GAS"
                    elif per_sender_cost.get(sender, 0) + cost > max_cost_sender:
                        expected = "E_SENDER_COST"
                    elif total_gas + gas > max_gas:
                        expected = "E_BUNDLE_GAS"
                    else:
                        self.assertGreater(total_cost + cost, max_cost)
                        expected = "E_BUNDLE_BUDGET"
                self.assertEqual(
                    skipped_reasons[index], expected, (doc, plan, index)
                )


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
            "0x" + "AB" * 20,  # 非规范小写
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

    def test_empty_entries_ok(self):
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

    def test_values_must_be_below_2_to_the_64(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": str(NONCE_MOD), "lastSequence": "0"}]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/nonceKey"
        )

        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": "0", "lastSequence": str(NONCE_MOD)}]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/0/lastSequence"
        )

        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(NONCE_MOD - 1, NONCE_MOD - 1)]
        }
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_duplicate_nonce_key(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(1, 0), state_entry(1, 2)]
        }
        self.assert_state_error(
            doc, "/senderNonceState/" + SENDER_A + "/1/nonceKey"
        )

    def test_same_nonce_key_for_different_senders_ok(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [state_entry(1, 0)],
            SENDER_B: [state_entry(1, 2)],
        }
        self.assertTrue(plan_bundle_sender_budget_with_nonce_state(doc)["ok"])

    def test_state_checked_after_usage(self):
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
        keys = [
            "requests",
            "sponsorshipPolicy",
            "bundlePolicy",
            "senderBudgetPolicy",
            "senderUsage",
        ]
        doc: dict = {}
        for key in keys:
            doc[key] = {} if key != "requests" else []
            err = error_of(plan_bundle_sender_budget_with_nonce_state(doc))
            following = keys[keys.index(key) + 1] if key != keys[-1] else None
            self.assertEqual(
                err["path"],
                "/" + (following if following is not None else "senderNonceState"),
            )

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
            [
                make_request(nonce=nonce_of(0, 3)),
                make_request(nonce=nonce_of(0, 4)),
                make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
            ],
            bundle_gas=ITEM_GAS * 3,
            bundle_cost=ITEM_COST * 3,
            usage={SENDER_A: usage_entry(1, 1)},
            nonce_state={SENDER_A: [state_entry(0, 2)]},
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1, 2])

    def test_nonce_gap_exit_0(self):
        doc = document(
            [make_request(nonce=nonce_of(0, 1))],
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
        )
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["plan"]["skipped"],
            [{"index": 0, "reason": "E_NONCE_GAP"}],
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
