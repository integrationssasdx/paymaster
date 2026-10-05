"""paymaster.plan_bundle_nonce_chain 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_nonce_chain -v
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
    _choose_nonce_chain,
    plan_bundle_nonce_chain,
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


def chain_ok(sequences: list[int]) -> bool:
    """按下标递增给出的 sequence 序列是否构成连续链。"""
    return all(b == a + 1 for a, b in zip(sequences, sequences[1:]))


class TestChooseNonceChain(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, candidates, max_gas, max_cost):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            groups = {}
            for i in chosen:
                groups.setdefault(candidates[i][:2], []).append(candidates[i][2])
            # chosen 按候选序号递增，各组 sequence 序列即下标递增顺序。
            if not all(chain_ok(seqs) for seqs in groups.values()):
                continue
            gas = sum(candidates[i][3] for i in chosen)
            cost = sum(candidates[i][4] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            key = (-len(chosen), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        return best

    def test_empty(self):
        self.assertEqual(_choose_nonce_chain([], 10, 10), ((), 0, 0))

    def test_none_fit_limits(self):
        candidates = [("a", 0, 0, 5, 5), ("a", 0, 1, 6, 1)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 4, 100)
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_contiguous_chain_selected(self):
        candidates = [("a", 0, 0, 2, 2), ("a", 0, 1, 3, 3), ("a", 0, 2, 4, 4)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1, 2), 9, 9))

    def test_gap_breaks_chain(self):
        # sequence 0 与 2 不相邻，不能同组入选；数量并列取 gas 较小者。
        candidates = [("a", 0, 0, 2, 2), ("a", 0, 2, 3, 1)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0,), 2, 2))

    def test_duplicate_sequence_keeps_one(self):
        candidates = [("a", 0, 1, 2, 2), ("a", 0, 1, 2, 2), ("a", 0, 1, 2, 2)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0,), 2, 2))

    def test_index_order_must_match_sequence_order(self):
        # 下标递增时 sequence 为 1、0，不构成链；只能取一项，取下标较小者。
        candidates = [("a", 0, 1, 2, 2), ("a", 0, 0, 2, 2)]
        chosen, _, _ = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual(chosen, (0,))

    def test_run_can_start_later(self):
        # 0 号 sequence 5 与 1、2 号（sequence 1、2）各自成链；取数量大的链。
        candidates = [("a", 0, 5, 1, 1), ("a", 0, 1, 2, 2), ("a", 0, 2, 3, 3)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((1, 2), 5, 5))

    def test_same_sequence_different_keys_coexist(self):
        candidates = [("a", 0, 0, 2, 2), ("a", 1, 0, 3, 3), ("b", 0, 0, 4, 4)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1, 2), 9, 9))

    def test_count_outranks_metrics(self):
        # 0 号独占一条贵链；1、2 号构成便宜的两项链：取两项方案。
        candidates = [
            ("a", 0, 0, 1, 100),
            ("a", 0, 7, 5, 1),
            ("a", 0, 8, 5, 1),
        ]
        chosen, _, _ = _choose_nonce_chain(candidates, 10, 2)
        self.assertEqual(chosen, (1, 2))

    def test_gas_tiebreak_picks_variant(self):
        # 同 sequence 两个变体只能取一个：gas 小者胜，即便其下标更大。
        candidates = [("a", 0, 3, 5, 1), ("a", 0, 3, 2, 9)]
        chosen, gas, cost = _choose_nonce_chain(candidates, 4, 20)
        self.assertEqual((chosen, gas, cost), ((1,), 2, 9))

    def test_lexicographic_final_tiebreak(self):
        candidates = [
            ("a", 0, 0, 2, 2),
            ("b", 0, 0, 2, 2),
            ("c", 0, 0, 2, 2),
        ]
        chosen, _, _ = _choose_nonce_chain(candidates, 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261004)
        keys = [("a", 0), ("a", 1), ("b", 0), ("b", 1), ("c", 0)]
        for _ in range(3000):
            count = rng.randint(0, 9)
            candidates = [
                (*rng.choice(keys), rng.randint(0, 4),
                 rng.randint(1, 7), rng.randint(1, 40))
                for _ in range(count)
            ]
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_nonce_chain(candidates, max_gas, max_cost)
            want = self.brute_force(candidates, max_gas, max_cost)
            self.assertEqual(got, want, (candidates, max_gas, max_cost, got, want))


class TestPlanEndToEnd(unittest.TestCase):
    def test_chain_selected_when_capacity_ample(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x2"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "3")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 3))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 3))

    def test_empty_requests_succeeds(self):
        plan = plan_of(
            plan_bundle_nonce_chain(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_duplicate_sequence_conflict(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x5"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 1, "reason": "E_NONCE_CONFLICT"},
                {"index": 2, "reason": "E_NONCE_CONFLICT"},
            ],
        )

    def test_normalized_sender_case_and_nonce_case_conflict(self):
        reqs = [
            make_request(sender="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                         nonce="0xa"),
            # 同地址（大小写不同）同 nonce（0xA 规范化为 0xa）→ 同 sequence 冲突。
            make_request(sender="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                         nonce="0xA"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_gap_in_sequence_marked_gap(self):
        # 0、2 号构成 sequence 0、1 的链；1 号 sequence 5 不接链 → E_NONCE_GAP。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_index_order_violation_marked_gap(self):
        # 下标递增时 sequence 为 1、0：0 号入选后 1 号只能接在前端，但其下标
        # 更大 → 破坏连续链。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_nonce_key_split_at_2_pow_64(self):
        # nonce 2^64-1（key 0, sequence 2^64-1）与 2^64（key 1, sequence 0）
        # 不同组，互不构成链约束，可同时入选。
        reqs = [
            make_request(sender=SENDER_A, nonce=hex(MOD - 1)),
            make_request(sender=SENDER_A, nonce=hex(MOD)),
            make_request(sender=SENDER_A, nonce=hex(MOD + 1)),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])

    def test_same_key_chain_across_boundary_values(self):
        # key 1 组内 sequence 0、1 构成链；与 key 0 组互不影响。
        reqs = [
            make_request(sender=SENDER_A, nonce=hex(MOD)),
            make_request(sender=SENDER_A, nonce=hex(MOD + 1)),
            make_request(sender=SENDER_B, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])

    def test_longest_chain_wins(self):
        # sequence 0、1、2 的三项链优于 sequence 5 的单项。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x2"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [1, 2, 3])
        self.assertEqual(plan["skipped"],
                         [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_chain_limited_by_gas(self):
        # 限额只容两项：取 sequence 0、1 的链，sequence 2 接链但 gas 超限。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x2"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"],
                         [{"index": 2, "reason": "E_BUNDLE_GAS"}])

    def test_chain_limited_by_budget(self):
        # 成本只容两项：sequence 2 接链且 gas 不超，成本超限。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x2"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"],
                         [{"index": 2, "reason": "E_BUNDLE_BUDGET"}])

    def test_gap_takes_precedence_over_gas(self):
        # 1 号既破坏链又超 gas：记 E_NONCE_GAP。
        big = make_request(call_gas=0x20000, sender=SENDER_A, nonce="0x9")
        reqs = [make_request(sender=SENDER_A, nonce="0x0"), big]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_GAP"}])

    def test_conflict_takes_precedence_over_gap_and_gas(self):
        # 1 号与入选的 0 号同 sequence，即便也超 gas 仍记冲突。
        small = make_request(call_gas=0x1000, sender=SENDER_A, nonce="0x1")
        huge = make_request(call_gas=0x20000, sender=SENDER_A, nonce="0x1")
        small_gas, _ = request_profile(small)
        doc = document([small, huge], bundle_gas=small_gas,
                       bundle_cost=ITEM_COST * 2)
        plan = plan_of(plan_bundle_nonce_chain(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_unapproved_requests_keep_reason_and_position(self):
        gas_rejected = make_request(sender=SENDER_A, nonce="0x0")
        gas_rejected["userOperation"]["preVerificationGas"] = "0x200000"
        budget_rejected = make_request(max_fee=0xDE0B6B3A7640000,
                                       sender=SENDER_B, nonce="0x0")
        ok_req = make_request(sender=SENDER_A, nonce="0x1")
        doc = document(
            [gas_rejected, ok_req, budget_rejected],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_nonce_chain(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_GAS_LIMIT"},
                {"index": 2, "reason": "E_BUDGET"},
            ],
        )

    def test_single_oversized_request_marked_gas(self):
        plan = plan_of(
            plan_bundle_nonce_chain(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_gas_checked_before_budget_for_excluded(self):
        plan = plan_of(
            plan_bundle_nonce_chain(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST - 1)
            )
        )
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_selected_and_skipped_sorted_and_strings_decimal(self):
        reqs = [make_request(sender=SENDER_A, nonce="0x0"),
                make_request(sender=SENDER_A, nonce="0x1"),
                make_request(sender=SENDER_A, nonce="0x1"),
                make_request(sender=SENDER_A, nonce="0x7")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 3, bundle_cost=ITEM_COST * 3)
        plan = plan_of(plan_bundle_nonce_chain(doc))
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
        reqs = [make_request(sender=SENDER_A, nonce="0x0"),
                make_request(sender=SENDER_A, nonce="0x0")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
        snapshot = copy.deepcopy(doc)
        plan_bundle_nonce_chain(doc)
        self.assertEqual(doc, snapshot)

    def test_random_instances_optimality_and_reasons(self):
        """随机端到端：与穷举最优一致，且落选原因与最终组状态一致。"""
        rng = random.Random(99)
        senders = (SENDER_A, SENDER_B, SENDER_C)
        for _ in range(300):
            count = rng.randint(1, 7)
            reqs = []
            profiles = []
            keys = []
            for i in range(count):
                req = make_request(
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000, 0x20000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10, 0x40]),
                    sender=rng.choice(senders),
                    nonce=hex(rng.randint(0, 4)),
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
            max_gas = rng.randint(ITEM_GAS // 2, ITEM_GAS * 3)
            max_cost = rng.randint(ITEM_COST // 2, ITEM_COST * 3)
            doc = document(reqs, bundle_gas=max_gas, bundle_cost=max_cost)
            plan = plan_of(plan_bundle_nonce_chain(doc))

            selected = plan["selected"]
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            # 入选组满足链约束。
            groups = {}
            for i in selected:
                groups.setdefault(keys[i][:2], []).append(keys[i][2])
            for seqs in groups.values():
                self.assertTrue(chain_ok(seqs), (doc, plan))

            # 与穷举最优（数量、gas、cost）一致。
            best_key = None
            for mask in range(1 << count):
                chosen_idx = [i for i in range(count) if mask >> i & 1]
                chosen_groups = {}
                for i in chosen_idx:
                    chosen_groups.setdefault(keys[i][:2], []).append(keys[i][2])
                if not all(chain_ok(s) for s in chosen_groups.values()):
                    continue
                gas = sum(profiles[i][0] for i in chosen_idx)
                cost = sum(profiles[i][1] for i in chosen_idx)
                if gas > max_gas or cost > max_cost:
                    continue
                key = (-len(chosen_idx), gas, cost)
                if best_key is None or key < best_key:
                    best_key = key
            assert best_key is not None  # 空集恒可行
            best_count, best_gas, best_cost = (
                -best_key[0], best_key[1], best_key[2]
            )
            self.assertEqual(len(selected), best_count, (doc, plan))
            self.assertEqual(total_gas, best_gas)
            self.assertEqual(total_cost, best_cost)

            # 落选原因与最终组状态一致。
            chosen_sequences = {}
            for i in selected:
                chosen_sequences.setdefault(keys[i][:2], {})[keys[i][2]] = i
            for item in plan["skipped"]:
                if item["reason"] in ("E_GAS_LIMIT", "E_BUDGET"):
                    continue
                index = item["index"]
                sender, key, sequence = keys[index]
                gas, cost = profiles[index]
                sequences = chosen_sequences.get((sender, key))
                if sequences is not None and sequence in sequences:
                    self.assertEqual(item["reason"], "E_NONCE_CONFLICT",
                                     (doc, item))
                    continue
                extends = sequences is None or (
                    sequence == min(sequences) - 1
                    and index < sequences[min(sequences)]
                ) or (
                    sequence == max(sequences) + 1
                    and index > sequences[max(sequences)]
                )
                if not extends:
                    self.assertEqual(item["reason"], "E_NONCE_GAP", (doc, item))
                elif total_gas + gas > max_gas:
                    self.assertEqual(item["reason"], "E_BUNDLE_GAS", (doc, item))
                else:
                    self.assertGreater(total_cost + cost, max_cost)
                    self.assertEqual(item["reason"], "E_BUNDLE_BUDGET",
                                     (doc, item))


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
                err = error_of(plan_bundle_nonce_chain(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(
            plan_bundle_nonce_chain({"requests": [], "extra": 1})
        )
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_nonce_chain({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_nonce_chain({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_nonce_chain({"requests": [], "sponsorshipPolicy": {}})
        )
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_nonce_chain(doc))
                self.assertEqual((err["code"], err["path"]),
                                 (E_INVALID_FIELD, "/requests"))

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["nonce"]
        err = error_of(plan_bundle_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_MISSING_FIELD, "/requests/1/userOperation/nonce"))

    def test_policy_errors(self):
        doc = self.valid_doc()
        doc["bundlePolicy"] = {}
        err = error_of(plan_bundle_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_MISSING_FIELD, "/bundlePolicy/maxTotalGas"))

        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        err = error_of(plan_bundle_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/sponsorshipPolicy"))

        doc = self.valid_doc()
        doc["bundlePolicy"]["maxCostWei"] = "0x0"
        err = error_of(plan_bundle_nonce_chain(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxCostWei"))

    def test_result_shapes(self):
        result = plan_bundle_nonce_chain({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_nonce_chain(document([], bundle_gas=1, bundle_cost=1))
        self.assertTrue(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "plan"})
        self.assertEqual(
            set(result["plan"].keys()),
            {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"},
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_nonce_chain"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        doc = document(
            reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1])

    def test_gap_exit_0(self):
        reqs = [make_request(nonce="0x0"), make_request(nonce="0x2")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])
        self.assertEqual(result["plan"]["skipped"],
                         [{"index": 1, "reason": "E_NONCE_GAP"}])

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
