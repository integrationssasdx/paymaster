"""paymaster.plan_bundle_sender_nonce_chain 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_sender_nonce_chain -v
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
    _choose_sender_nonce_chain,
    plan_bundle_sender_nonce_chain,
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

# nonce 链分组的模数：key = nonce // MOD，sequence = nonce % MOD。
MOD = 1 << 64


def make_request(
    call_gas: int = 0x10000,
    max_fee: int = 0x10,
    sender: str = SENDER_A,
    nonce: str = "0x0",
) -> dict:
    return {
        "userOperation": {
            "sender": sender,
            "nonce": nonce,
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
             max_per_sender: int = 0x1000,
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
        "fairnessPolicy": {
            "maxPerSender": hex(max_per_sender),
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


def chain_ok(sequences: list[int]) -> bool:
    """按下标递增给出的 sequence 序列是否构成连续链。"""
    return all(b == a + 1 for a, b in zip(sequences, sequences[1:]))


class TestChooseSenderNonceChain(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, candidates, quota, max_gas, max_cost):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            groups: dict = {}
            counts: dict = {}
            for i in chosen:
                sender, key, sequence = candidates[i][:3]
                groups.setdefault((sender, key), []).append(sequence)
                counts[sender] = counts.get(sender, 0) + 1
            if any(count > quota for count in counts.values()):
                continue
            # chosen 按候选序号递增，各组 sequence 序列即下标递增顺序。
            if not all(chain_ok(seqs) for seqs in groups.values()):
                continue
            gas = sum(candidates[i][3] for i in chosen)
            cost = sum(candidates[i][4] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            key = (-len(chosen), -len(counts), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        return best

    def test_empty(self):
        self.assertEqual(_choose_sender_nonce_chain([], 1, 10, 10), ((), 0, 0))

    def test_none_fit_limits(self):
        candidates = [("a", 0, 0, 5, 5), ("a", 0, 1, 6, 1)]
        chosen, gas, cost = _choose_sender_nonce_chain(candidates, 2, 4, 100)
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_contiguous_chain_selected(self):
        candidates = [
            ("a", 0, 0, 2, 2),
            ("a", 0, 1, 3, 3),
            ("a", 0, 2, 4, 4),
        ]
        chosen, gas, cost = _choose_sender_nonce_chain(candidates, 3, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1, 2), 9, 9))

    def test_quota_caps_across_keys(self):
        # 同一 sender 两个不同 key 各两项，配额 2：跨 key 总数受限。
        candidates = [
            ("a", 0, 0, 2, 2),
            ("a", 0, 1, 2, 2),
            ("a", 1, 0, 2, 2),
            ("a", 1, 1, 2, 2),
        ]
        chosen, gas, cost = _choose_sender_nonce_chain(candidates, 2, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1), 4, 4))

    def test_independent_keys_share_quota(self):
        # 配额 1：不同 key 相互独立，可各取一个 sender 链项，但同 sender 总数 1。
        candidates = [
            ("a", 0, 0, 2, 2),
            ("a", 1, 0, 2, 2),
            ("b", 0, 0, 2, 2),
        ]
        chosen, gas, cost = _choose_sender_nonce_chain(candidates, 1, 100, 100)
        # 数量 2 并列时取不同 sender 数更大者：a 与 b 各一个。
        self.assertEqual(chosen, (0, 2))
        self.assertEqual((gas, cost), (4, 4))

    def test_gap_breaks_chain(self):
        candidates = [("a", 0, 0, 2, 2), ("a", 0, 2, 3, 1)]
        chosen, gas, cost = _choose_sender_nonce_chain(candidates, 2, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0,), 2, 2))

    def test_duplicate_sequence_keeps_one(self):
        candidates = [
            ("a", 0, 1, 2, 2),
            ("a", 0, 1, 2, 2),
            ("a", 0, 1, 2, 2),
        ]
        chosen, gas, cost = _choose_sender_nonce_chain(candidates, 3, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0,), 2, 2))

    def test_distinct_senders_outrank_gas(self):
        # 数量 2 并列（三项共 gas 11，限额 10 只容两项）：{0,1} 同 sender 但
        # gas 小；{0,2} 两个 sender 但 gas 大 → 不同 sender 数优先，取 {0,2}。
        candidates = [
            ("a", 0, 0, 1, 1),
            ("a", 0, 1, 1, 1),
            ("b", 0, 0, 9, 9),
        ]
        chosen, _, _ = _choose_sender_nonce_chain(candidates, 2, 10, 20)
        self.assertEqual(chosen, (0, 2))

    def test_gas_then_cost_tiebreak(self):
        candidates = [
            ("a", 0, 0, 3, 5),
            ("b", 0, 0, 4, 1),
            ("c", 0, 0, 5, 1),
        ]
        chosen, gas, _ = _choose_sender_nonce_chain(candidates, 1, 5, 10)
        self.assertEqual((chosen, gas), ((0,), 3))

    def test_lexicographic_final_tiebreak(self):
        candidates = [
            ("a", 0, 0, 2, 2),
            ("b", 0, 0, 2, 2),
            ("c", 0, 0, 2, 2),
        ]
        chosen, _, _ = _choose_sender_nonce_chain(candidates, 1, 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261005)
        keys_pool = [
            ("a", 0), ("a", 1), ("a", 2),
            ("b", 0), ("b", 1),
            ("c", 0),
        ]
        for _ in range(4000):
            count = rng.randint(0, 9)
            candidates = []
            for _ in range(count):
                sender, key = rng.choice(keys_pool)
                candidates.append(
                    (sender, key, rng.randint(0, 4),
                     rng.randint(1, 7), rng.randint(1, 40))
                )
            quota = rng.randint(1, 3)
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_sender_nonce_chain(
                candidates, quota, max_gas, max_cost
            )
            want = self.brute_force(candidates, quota, max_gas, max_cost)
            self.assertEqual(
                got, want, (candidates, quota, max_gas, max_cost, got, want)
            )


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_capacity_ample(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_B, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "3")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 3))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 3))

    def test_empty_requests_succeeds(self):
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
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
        req["userOperation"]["preVerificationGas"] = "0x200000"
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document([req], bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["operationCount"], "0")

    def test_duplicate_sequence_conflict(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x5"),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10, max_per_sender=2)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_gap_marked_gap(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10, max_per_sender=3)
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_quota_shared_across_keys(self):
        # 同一 sender 三个不同 key 各一项，互不构成链约束，但配额 2：第三项记
        # E_SENDER_QUOTA（不断链：其组内尚无入选项）。
        reqs = [
            make_request(sender=SENDER_A, nonce=hex(MOD)),
            make_request(sender=SENDER_A, nonce=hex(2 * MOD)),
            make_request(sender=SENDER_A, nonce=hex(3 * MOD)),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10, max_per_sender=2)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"],
                         [{"index": 2, "reason": "E_SENDER_QUOTA"}])

    def test_conflict_before_gap_before_quota(self):
        # 1 号与入选的 0 号同 (sender,key,sequence)：即便 sender 已达配额且并入
        # 也断链，仍优先记 E_NONCE_CONFLICT。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST,
                         max_per_sender=1)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_gap_before_quota(self):
        # 0、2 号构成 sequence 0、1 的链；1 号 sequence 5：并入断链（其 sender
        # 也已达配额 2），优先记 E_NONCE_GAP。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10, max_per_sender=2)
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_quota_before_gas(self):
        # 同一 sender 不同 key 两项，配额 1，gas 也只够一项：记 E_SENDER_QUOTA。
        reqs = [
            make_request(sender=SENDER_A, nonce=hex(MOD)),
            make_request(sender=SENDER_A, nonce=hex(2 * MOD)),
        ]
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST,
                         max_per_sender=1)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_SENDER_QUOTA"}])

    def test_gas_before_budget(self):
        cheap = make_request(max_fee=0x1, sender=SENDER_A, nonce=hex(MOD))
        pricey = make_request(max_fee=0x100, sender=SENDER_B, nonce=hex(2 * MOD))
        cheap_gas, cheap_cost = request_profile(cheap)
        # gas 只容一项、成本只容便宜项：1 号 sender 未满，并入先超 gas。
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document([cheap, pricey], bundle_gas=cheap_gas,
                         bundle_cost=cheap_cost, max_per_sender=1)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_budget_reason_when_gas_fits(self):
        cheap = make_request(max_fee=0x1, sender=SENDER_A, nonce=hex(MOD))
        pricey = make_request(max_fee=0x100, sender=SENDER_B, nonce=hex(2 * MOD))
        cheap_gas, cheap_cost = request_profile(cheap)
        pricey_gas, _ = request_profile(pricey)
        plan = plan_of(
            plan_bundle_sender_nonce_chain(
                document([cheap, pricey],
                         bundle_gas=cheap_gas + pricey_gas,
                         bundle_cost=cheap_cost, max_per_sender=1)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_BUNDLE_BUDGET"}])

    def test_unapproved_requests_keep_reason_and_position(self):
        gas_rejected = make_request(sender=SENDER_A, nonce="0x0")
        gas_rejected["userOperation"]["preVerificationGas"] = "0x200000"
        budget_rejected = make_request(max_fee=0xDE0B6B3A7640000,
                                       sender=SENDER_B, nonce="0x0")
        ok_req = make_request(sender=SENDER_C, nonce="0x1")
        doc = document(
            [gas_rejected, ok_req, budget_rejected],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_sender_nonce_chain(doc))
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
            [
                make_request(sender=SENDER_A, nonce="0x0"),
                make_request(sender=SENDER_A, nonce="0x1"),
                make_request(sender=SENDER_A, nonce="0x1"),
                make_request(sender=SENDER_A, nonce="0x7"),
            ],
            bundle_gas=ITEM_GAS * 3, bundle_cost=ITEM_COST * 3,
            max_per_sender=4,
        )
        plan = plan_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual([item["index"] for item in plan["skipped"]], [2, 3])
        for item in plan["skipped"]:
            self.assertEqual(set(item.keys()), {"index", "reason"})
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        doc = document(
            [
                make_request(sender=SENDER_A, nonce="0x0"),
                make_request(sender=SENDER_A, nonce="0x1"),
            ],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_sender_nonce_chain(doc)
        self.assertEqual(doc, snapshot)

    def test_random_instances_optimality_and_reasons(self):
        """随机端到端：与穷举最优一致，落选原因与最终组状态一致。"""
        rng = random.Random(31337)
        senders = (SENDER_A, SENDER_B, SENDER_C)
        for _ in range(400):
            count = rng.randint(1, 7)
            reqs = []
            profiles = []
            keys = []
            for _ in range(count):
                req = make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000, 0x20000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10, 0x40]),
                    sender=rng.choice(senders),
                    nonce=hex(rng.randint(0, 6)),
                )
                reqs.append(req)
                profiles.append(request_profile(req))
                nonce = int(req["userOperation"]["nonce"], 16)
                keys.append(
                    (
                        req["userOperation"]["sender"].lower(),
                        nonce // MOD,
                        nonce % MOD,
                    )
                )
            quota = rng.randint(1, 3)
            max_gas = rng.randint(ITEM_GAS // 2, ITEM_GAS * 3)
            max_cost = rng.randint(ITEM_COST // 2, ITEM_COST * 3)
            doc = document(
                reqs, bundle_gas=max_gas, bundle_cost=max_cost,
                max_per_sender=quota,
            )
            plan = plan_of(plan_bundle_sender_nonce_chain(doc))

            selected = plan["selected"]
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)

            # 入选组满足链约束与 sender 配额。
            chain_groups: dict = {}
            sender_counts: dict = {}
            for i in selected:
                chain_groups.setdefault(keys[i][:2], []).append(keys[i][2])
                sender = keys[i][0]
                sender_counts[sender] = sender_counts.get(sender, 0) + 1
                self.assertLessEqual(sender_counts[sender], quota)
            for seqs in chain_groups.values():
                self.assertTrue(chain_ok(seqs), (doc, plan))

            # 与穷举最优（数量、不同 sender、gas、cost）一致。
            best_key = None
            for mask in range(1 << count):
                chosen_idx = [i for i in range(count) if mask >> i & 1]
                cg: dict = {}
                cc: dict = {}
                for i in chosen_idx:
                    cg.setdefault(keys[i][:2], []).append(keys[i][2])
                    cc[keys[i][0]] = cc.get(keys[i][0], 0) + 1
                if any(v > quota for v in cc.values()):
                    continue
                if not all(chain_ok(s) for s in cg.values()):
                    continue
                gas = sum(profiles[i][0] for i in chosen_idx)
                cost = sum(profiles[i][1] for i in chosen_idx)
                if gas > max_gas or cost > max_cost:
                    continue
                key = (-len(chosen_idx), -len(cc), gas, cost)
                if best_key is None or key < best_key:
                    best_key = key
            assert best_key is not None  # 空集恒可行
            best_count, best_senders, best_gas, best_cost = (
                -best_key[0], -best_key[1], best_key[2], best_key[3]
            )
            self.assertEqual(len(selected), best_count, (doc, plan))
            self.assertEqual(len(sender_counts), best_senders, (doc, plan))
            self.assertEqual(total_gas, best_gas, (doc, plan))
            self.assertEqual(total_cost, best_cost, (doc, plan))

            # 落选原因与最终组状态、既定优先级一致。
            chosen_sequences: dict = {}
            for i in selected:
                chosen_sequences.setdefault(keys[i][:2], {})[keys[i][2]] = i
            reasons = {item["index"]: item["reason"] for item in plan["skipped"]}
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                # approved=false 的项不参与下面的结构判断。
                reason = reasons.get(index)
                if reason in ("E_GAS_LIMIT", "E_BUDGET"):
                    continue
                sender, key, sequence = keys[index]
                sequences = chosen_sequences.get((sender, key))
                if sequences is not None and sequence in sequences:
                    self.assertEqual(reason, "E_NONCE_CONFLICT", (doc, index))
                    continue
                extends = sequences is None or (
                    sequence == min(sequences) - 1
                    and index < sequences[min(sequences)]
                ) or (
                    sequence == max(sequences) + 1
                    and index > sequences[max(sequences)]
                )
                if not extends:
                    self.assertEqual(reason, "E_NONCE_GAP", (doc, index))
                elif sender_counts.get(sender, 0) >= quota:
                    self.assertEqual(reason, "E_SENDER_QUOTA", (doc, index))
                elif total_gas + gas > max_gas:
                    self.assertEqual(reason, "E_BUNDLE_GAS", (doc, index))
                else:
                    self.assertGreater(total_cost + cost, max_cost)
                    self.assertEqual(reason, "E_BUNDLE_BUDGET", (doc, index))


class TestStructureErrorsParity(unittest.TestCase):
    """结构错误码、path 与检查顺序与 plan_bundle_sender_fair 一致。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10,
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle_sender_nonce_chain(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(
            plan_bundle_sender_nonce_chain({"requests": [], "extra": 1})
        )
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_sender_nonce_chain({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_sender_nonce_chain({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_sender_nonce_chain(
                {"requests": [], "sponsorshipPolicy": {}}
            )
        )
        self.assertEqual(err["path"], "/bundlePolicy")
        err = error_of(
            plan_bundle_sender_nonce_chain(
                {
                    "requests": [],
                    "sponsorshipPolicy": {},
                    "bundlePolicy": {},
                }
            )
        )
        self.assertEqual(err["path"], "/fairnessPolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_sender_nonce_chain(doc))
                self.assertEqual((err["code"], err["path"]),
                                 (E_INVALID_FIELD, "/requests"))

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["nonce"]
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_MISSING_FIELD, "/requests/1/userOperation/nonce"))

    def test_policy_errors(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = {}
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_MISSING_FIELD, "/bundlePolicy/maxTotalGas"))

        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"))

        doc = self.valid_doc()
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxTotalGas"))

    def test_fairness_policy_errors(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"] = []
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/fairnessPolicy"))

        doc = self.valid_doc()
        doc["fairnessPolicy"] = {}
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_MISSING_FIELD, "/fairnessPolicy/maxPerSender"))

        doc = self.valid_doc()
        doc["fairnessPolicy"]["extra"] = "0x1"
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_UNKNOWN_FIELD, "/fairnessPolicy/extra"))

        doc = self.valid_doc()
        doc["fairnessPolicy"]["maxPerSender"] = "0x0"
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/fairnessPolicy/maxPerSender"))

        doc = self.valid_doc()
        doc["fairnessPolicy"]["maxPerSender"] = "2"
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/fairnessPolicy/maxPerSender"))

    def test_fairness_policy_checked_after_bundle_policy(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = []
        doc["fairnessPolicy"] = []
        err = error_of(plan_bundle_sender_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/bundlePolicy"))

    def test_result_shapes(self):
        result = plan_bundle_sender_nonce_chain({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_sender_nonce_chain(
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
            [sys.executable, "-m", "paymaster.pack_sender_nonce_chain"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_B, nonce="0x0"),
        ]
        doc = document(
            reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
            max_per_sender=1,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1])

    def test_quota_exit_0(self):
        reqs = [
            make_request(sender=SENDER_A, nonce=hex(MOD)),
            make_request(sender=SENDER_A, nonce=hex(2 * MOD)),
        ]
        doc = document(
            reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
            max_per_sender=1,
        )
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])
        self.assertEqual(result["plan"]["skipped"],
                         [{"index": 1, "reason": "E_SENDER_QUOTA"}])

    def test_structural_failure_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["requests"][0]["userOperation"]["sender"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_MISSING_FIELD)

    def test_missing_fairness_policy_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["fairnessPolicy"]
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_MISSING_FIELD)
        self.assertEqual(error["path"], "/fairnessPolicy")

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
