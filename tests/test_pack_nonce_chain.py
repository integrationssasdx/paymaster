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

NONCE_STEP = 1 << 64


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


def chain_feasible(chosen, candidates):
    """所选候选序号是否满足每个 (sender, key) 的 0..L-1 连续链约束。"""
    by_group: dict[tuple, list] = {}
    for pos in chosen:
        sender, key, sequence = candidates[pos][0], candidates[pos][1], candidates[pos][2]
        by_group.setdefault((sender, key), []).append((pos, sequence))
    for entries in by_group.values():
        # chosen 已按候选序号升序；同组内自然按下标递增。
        sequences = [sequence for _, sequence in entries]
        if sequences != list(range(len(sequences))):
            return False
    return True


class TestChooseNonceChain(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, candidates, max_gas, max_cost):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            if not chain_feasible(chosen, candidates):
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

    def cand(self, sender, key, sequence, gas, cost):
        return (sender, key, sequence, gas, cost)

    def test_empty(self):
        self.assertEqual(_choose_nonce_chain([], 10, 10), ((), 0, 0))

    def test_single_chain_in_order(self):
        candidates = [
            self.cand("a", 0, 0, 2, 2),
            self.cand("a", 0, 1, 3, 3),
            self.cand("a", 0, 2, 4, 4),
        ]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1, 2), 9, 9))

    def test_missing_sequence_zero_cannot_select_one(self):
        # 只有 sequence 1，没有 sequence 0：链必须从 0 起，无法入选。
        candidates = [self.cand("a", 0, 1, 2, 2)]
        self.assertEqual(_choose_nonce_chain(candidates, 100, 100), ((), 0, 0))

    def test_gap_blocks_tail(self):
        # 0、1、3：3 与 1 不相邻，最长链只取前两项。
        candidates = [
            self.cand("a", 0, 0, 2, 2),
            self.cand("a", 0, 1, 2, 2),
            self.cand("a", 0, 3, 2, 2),
        ]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1), 4, 4))

    def test_sequence_out_of_index_order_blocks(self):
        # 下标顺序里先出现 seq1 再出现 seq0：无法构成 0 后接 1 的递增下标链。
        candidates = [
            self.cand("a", 0, 1, 2, 2),
            self.cand("a", 0, 0, 2, 2),
        ]
        self.assertEqual(_choose_nonce_chain(candidates, 100, 100), ((1,), 2, 2))

    def test_duplicate_sequence_variants_both_considered(self):
        # seq0 两个变体，各可接 seq1：取指标更优的完整链。
        # 变体 0 号 gas=5，1 号 gas=1；2 号 seq1 gas=1。最优 (1,2)。
        candidates = [
            self.cand("a", 0, 0, 5, 5),
            self.cand("a", 0, 0, 1, 1),
            self.cand("a", 0, 1, 1, 1),
        ]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((1, 2), 2, 2))

    def test_duplicate_sequence_earlier_variant_needed_for_later(self):
        # seq1 出现在第二个 seq0 之前：只有第一个 seq0 能接上它，即使更贵。
        candidates = [
            self.cand("a", 0, 0, 5, 5),   # pos0 seq0
            self.cand("a", 0, 1, 1, 1),   # pos1 seq1
            self.cand("a", 0, 0, 1, 1),   # pos2 seq0（更便宜但接不上 pos1）
        ]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1), 6, 6))

    def test_distinct_keys_and_senders_independent(self):
        # 不同 sender 或不同 key 即便 sequence 相同也互不约束。
        candidates = [
            self.cand("a", 0, 0, 2, 2),
            self.cand("a", 1, 0, 2, 2),
            self.cand("b", 0, 0, 2, 2),
        ]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1, 2), 6, 6))

    def test_high_nonce_splits_key_and_sequence(self):
        candidates = [
            self.cand("a", 1, 0, 2, 2),  # nonce = 2**64
            self.cand("a", 1, 1, 2, 2),  # nonce = 2**64 + 1
            self.cand("a", 0, 0, 2, 2),  # nonce = 0（不同 key）
        ]
        chosen, gas, cost = _choose_nonce_chain(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1, 2), 6, 6))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261004)
        groups_meta = [("a", 0), ("a", 1), ("b", 0), ("c", 0)]
        for _ in range(4000):
            count = rng.randint(0, 8)
            candidates = []
            for _ in range(count):
                sender, key = rng.choice(groups_meta)
                sequence = rng.randint(0, 3)
                candidates.append(
                    self.cand(
                        sender, key, sequence,
                        rng.randint(1, 7), rng.randint(1, 40),
                    )
                )
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_nonce_chain(candidates, max_gas, max_cost)
            want = self.brute_force(candidates, max_gas, max_cost)
            self.assertEqual(got, want, (candidates, max_gas, max_cost, got, want))


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_chain_complete(self):
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

    def test_empty_requests_succeeds(self):
        plan = plan_of(
            plan_bundle_nonce_chain(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_gap_tail_marked_gap(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x3"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_NONCE_GAP"}])

    def test_missing_zero_marked_gap(self):
        reqs = [make_request(sender=SENDER_A, nonce="0x5")]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_out_of_index_order_marked_gap(self):
        # 0 号 seq1、1 号 seq0：选 1 号作为链首，0 号因下标早于链首无法并入。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

    def test_duplicate_sequence_marked_conflict(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        # 0 号占 seq0，2 号 seq1 接链；1 号重复 seq0 → 冲突。
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_duplicate_sequence_variant_picked_for_best_chain(self):
        # 便宜 seq0 在前（0 号 callGas 小），贵 seq0 在 1 号，2 号 seq1。
        light = make_request(call_gas=0x1000, sender=SENDER_A, nonce="0x0")
        heavy = make_request(call_gas=0x20000, sender=SENDER_A, nonce="0x0")
        tail = make_request(call_gas=0x1000, sender=SENDER_A, nonce="0x1")
        light_gas, _ = request_profile(light)
        tail_gas, _ = request_profile(tail)
        doc = document([light, heavy, tail],
                       bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
        plan = plan_of(plan_bundle_nonce_chain(doc))
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])
        self.assertEqual(plan["totalGas"], str(light_gas + tail_gas))

    def test_distinct_keys_coexist_and_split_64bit(self):
        reqs = [
            make_request(sender=SENDER_A, nonce=hex(NONCE_STEP)),       # key1 seq0
            make_request(sender=SENDER_A, nonce=hex(NONCE_STEP + 1)),   # key1 seq1
            make_request(sender=SENDER_A, nonce="0x0"),                 # key0 seq0
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])

    def test_different_senders_same_sequence_coexist(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_B, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])

    def test_gas_reason_for_appendable_next_sequence(self):
        # 链首 0 号入选；1 号是下一项 seq1 但并入超 gas → E_BUNDLE_GAS。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
        ]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_budget_reason_for_appendable_next_sequence(self):
        cheap = make_request(call_gas=0x1000, max_fee=0x1,
                            sender=SENDER_A, nonce="0x0")
        pricey = make_request(call_gas=0x1000, max_fee=0x100,
                              sender=SENDER_A, nonce="0x1")
        cheap_gas, cheap_cost = request_profile(cheap)
        _, pricey_cost = request_profile(pricey)
        doc = document(
            [cheap, pricey],
            bundle_gas=cheap_gas * 2 + 1,
            bundle_cost=cheap_cost + pricey_cost - 1,
        )
        plan = plan_of(plan_bundle_nonce_chain(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_BUNDLE_BUDGET"}])

    def test_gap_takes_precedence_over_gas(self):
        # 断链项即便并入也超 gas，仍记 GAP。
        reqs = [make_request(sender=SENDER_A, nonce="0x9")]
        plan = plan_of(
            plan_bundle_nonce_chain(
                document(reqs, bundle_gas=1, bundle_cost=1)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_NONCE_GAP"}])

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
        # ok_req 是 SENDER_A 的 seq1 但该 sender 无 seq0 入选 → GAP。
        self.assertEqual(plan["selected"], [])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_GAS_LIMIT"},
                {"index": 1, "reason": "E_NONCE_GAP"},
                {"index": 2, "reason": "E_BUDGET"},
            ],
        )

    def test_selected_and_skipped_sorted_and_strings_decimal(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_A, nonce="0x4"),
        ]
        doc = document(reqs, bundle_gas=ITEM_GAS * 3, bundle_cost=ITEM_COST * 3)
        plan = plan_of(plan_bundle_nonce_chain(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(
            [item["index"] for item in plan["skipped"]], [2, 3]
        )
        reasons = {item["index"]: item["reason"] for item in plan["skipped"]}
        self.assertEqual(reasons[2], "E_NONCE_CONFLICT")
        self.assertEqual(reasons[3], "E_NONCE_GAP")
        for item in plan["skipped"]:
            self.assertEqual(set(item.keys()), {"index", "reason"})
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        reqs = [make_request(sender=SENDER_A, nonce="0x0"),
                make_request(sender=SENDER_A, nonce="0x1")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
        snapshot = copy.deepcopy(doc)
        plan_bundle_nonce_chain(doc)
        self.assertEqual(doc, snapshot)

    def test_random_instances_optimality_and_reasons(self):
        """随机端到端：与穷举最优一致，且落选原因与最终组状态一致。"""
        rng = random.Random(31337)
        senders = (SENDER_A, SENDER_B, SENDER_C)

        for _ in range(400):
            count = rng.randint(1, 7)
            reqs = []
            profiles = []
            metas = []  # (sender_lower, key, sequence)
            for _ in range(count):
                call_gas = rng.choice([0x1000, 0x4000, 0x10000, 0x20000])
                max_fee = rng.choice([0x1, 0x8, 0x10, 0x40])
                sender = rng.choice(senders)
                nonce_int = rng.randint(0, 2 * NONCE_STEP + 2)
                req = make_request(
                    call_gas=call_gas, max_fee=max_fee,
                    sender=sender, nonce=hex(nonce_int),
                )
                reqs.append(req)
                profiles.append(request_profile(req))
                metas.append((
                    sender.lower(),
                    nonce_int // NONCE_STEP,
                    nonce_int % NONCE_STEP,
                ))
            max_gas = rng.randint(ITEM_GAS // 2, ITEM_GAS * 3)
            max_cost = rng.randint(ITEM_COST // 2, ITEM_COST * 3)
            doc = document(reqs, bundle_gas=max_gas, bundle_cost=max_cost)
            plan = plan_of(plan_bundle_nonce_chain(doc))

            selected = plan["selected"]
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            self.assertTrue(chain_feasible(tuple(selected), [
                (s, k, seq, 0, 0) for s, k, seq in metas
            ]))

            # 与穷举最优（数量、gas、cost）一致。
            best_key = None
            for mask in range(1 << count):
                chosen_idx = tuple(i for i in range(count) if mask >> i & 1)
                if not chain_feasible(
                    chosen_idx,
                    [(s, k, seq, 0, 0) for s, k, seq in metas],
                ):
                    continue
                gas = sum(profiles[i][0] for i in chosen_idx)
                cost = sum(profiles[i][1] for i in chosen_idx)
                if gas > max_gas or cost > max_cost:
                    continue
                key = (-len(chosen_idx), gas, cost)
                if best_key is None or key < best_key:
                    best_key = key
            assert best_key is not None
            self.assertEqual(len(selected), -best_key[0], (doc, plan))
            self.assertEqual(total_gas, best_key[1], (doc, plan))
            self.assertEqual(total_cost, best_key[2], (doc, plan))

            # 落选原因预言机。
            chains: dict[tuple, list[int]] = {}
            for idx in selected:
                chains.setdefault(metas[idx][:2], []).append(idx)
            for item in plan["skipped"]:
                if item["reason"] in ("E_GAS_LIMIT", "E_BUDGET"):
                    continue
                idx = item["index"]
                sender, key, seq = metas[idx]
                chain = chains.get((sender, key), [])
                chain_len = len(chain)
                gas, cost = profiles[idx]
                if seq < chain_len:
                    self.assertEqual(item["reason"], "E_NONCE_CONFLICT",
                                     (doc, item))
                else:
                    appendable = seq == chain_len and (
                        chain_len == 0 or idx > chain[-1]
                    )
                    if not appendable:
                        self.assertEqual(item["reason"], "E_NONCE_GAP",
                                         (doc, item))
                    elif total_gas + gas > max_gas:
                        self.assertEqual(item["reason"], "E_BUNDLE_GAS",
                                         (doc, item))
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

    def test_gap_still_ok_exit_0(self):
        reqs = [make_request(nonce="0x7")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(result["plan"]["skipped"],
                         [{"index": 0, "reason": "E_NONCE_GAP"}])

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
