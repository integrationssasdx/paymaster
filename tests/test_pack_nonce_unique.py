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
SENDER_C = "0x3333333333333333333333333333333333333333"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# 标准单笔请求的 totalGas = 0x10000 + 0x20000 + 0x30000 = 393216
ITEM_GAS = 393216
# 标准单笔请求的 estimatedCostWei = 393216 * 0x10 = 6291456
ITEM_COST = 6291456


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


class TestChooseNonceUnique(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, candidates, max_gas, max_cost):
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(candidates)):
            chosen = tuple(i for i in range(len(candidates)) if mask >> i & 1)
            keys = [candidates[i][:2] for i in chosen]
            if len(set(keys)) != len(keys):
                continue
            gas = sum(candidates[i][2] for i in chosen)
            cost = sum(candidates[i][3] for i in chosen)
            if gas > max_gas or cost > max_cost:
                continue
            key = (-len(chosen), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        return best

    def test_empty(self):
        self.assertEqual(_choose_nonce_unique([], 10, 10), ((), 0, 0))

    def test_none_fit_limits(self):
        candidates = [("a", 0, 5, 5), ("b", 0, 6, 1)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 4, 100)
        self.assertEqual((chosen, gas, cost), ((), 0, 0))

    def test_duplicate_key_keeps_one(self):
        candidates = [("a", 0, 2, 2), ("a", 0, 2, 2), ("a", 0, 2, 2)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0,), 2, 2))

    def test_same_nonce_different_senders_coexist(self):
        candidates = [("a", 0, 2, 2), ("b", 0, 3, 3)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1), 5, 5))

    def test_same_sender_different_nonces_coexist(self):
        candidates = [("a", 0, 2, 2), ("a", 1, 3, 3)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1), 5, 5))

    def test_count_outranks_metrics_under_conflict(self):
        # 0 号独占键 K0 但昂贵；1、2 号键不同且便宜：取两项方案。
        candidates = [
            ("a", 0, 1, 100),
            ("a", 1, 5, 1),
            ("b", 0, 5, 1),
        ]
        chosen, _, _ = _choose_nonce_unique(candidates, 10, 2)
        self.assertEqual(chosen, (1, 2))

    def test_gas_tiebreak_picks_variant(self):
        # 同键两个变体只能取一个：gas 小者胜，即便其下标更大。
        candidates = [("a", 0, 5, 1), ("a", 0, 2, 9)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 4, 20)
        self.assertEqual((chosen, gas, cost), ((1,), 2, 9))

    def test_cost_tiebreak_picks_variant(self):
        candidates = [("a", 0, 2, 5), ("a", 0, 2, 3)]
        chosen, gas, cost = _choose_nonce_unique(candidates, 2, 10)
        self.assertEqual((chosen, gas, cost), ((1,), 2, 3))

    def test_lexicographic_final_tiebreak(self):
        candidates = [
            ("a", 0, 2, 2),
            ("b", 0, 2, 2),
            ("c", 0, 2, 2),
        ]
        chosen, _, _ = _choose_nonce_unique(candidates, 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261004)
        keys = [("a", 0), ("a", 1), ("b", 0), ("b", 1), ("c", 0)]
        for _ in range(3000):
            count = rng.randint(0, 9)
            candidates = [
                (*rng.choice(keys), rng.randint(1, 7), rng.randint(1, 40))
                for _ in range(count)
            ]
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_nonce_unique(candidates, max_gas, max_cost)
            want = self.brute_force(candidates, max_gas, max_cost)
            self.assertEqual(got, want, (candidates, max_gas, max_cost, got, want))


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_capacity_ample(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_B, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_nonce_unique(
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
            plan_bundle_nonce_unique(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_duplicate_nonce_keeps_earliest_by_default(self):
        reqs = [
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x5"),
            make_request(sender=SENDER_A, nonce="0x5"),
        ]
        plan = plan_of(
            plan_bundle_nonce_unique(
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
        self.assertEqual(plan["operationCount"], "1")

    def test_normalized_sender_case_and_nonce_case_conflict(self):
        reqs = [
            make_request(sender="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                         nonce="0xa"),
            # 同地址（大小写不同）同 nonce（0xA 规范化为 0xa）→ 冲突。
            make_request(sender="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                         nonce="0xA"),
        ]
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(reqs, bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_conflict_prefers_lower_gas_variant(self):
        heavy = make_request(call_gas=0x20000, sender=SENDER_A, nonce="0x1")
        light = make_request(call_gas=0x1000, sender=SENDER_A, nonce="0x1")
        heavy_gas, _ = request_profile(heavy)
        light_gas, light_cost = request_profile(light)
        doc = document(
            [heavy, light],
            bundle_gas=max(heavy_gas, light_gas),
            bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"],
                         [{"index": 0, "reason": "E_NONCE_CONFLICT"}])
        self.assertEqual(plan["totalGas"], str(light_gas))
        self.assertEqual(plan["estimatedCostWei"], str(light_cost))

    def test_conflict_reason_takes_precedence_over_gas(self):
        # 入选组已含同键的小请求；同键的大请求即便并入也会超 gas，仍记冲突。
        small = make_request(call_gas=0x1000, sender=SENDER_A, nonce="0x1")
        huge = make_request(call_gas=0x20000, sender=SENDER_A, nonce="0x1")
        small_gas, _ = request_profile(small)
        doc = document([small, huge], bundle_gas=small_gas, bundle_cost=ITEM_COST * 2)
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

    def test_non_conflicting_excess_gas_marked_gas(self):
        # 两个不同键的相同请求，限额只容一个；落选者键不在组中，gas 先超。
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_B, nonce="0x0"),
        ]
        plan = plan_of(
            plan_bundle_nonce_unique(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 2)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"],
                         [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_non_conflicting_budget_excess_marked_budget(self):
        # 0 号大请求（不同键）gas 放得进但成本超限；1 号小请求入选。
        pricey = make_request(call_gas=0x10000, max_fee=0x100,
                              sender=SENDER_A, nonce="0x0")
        cheap = make_request(call_gas=0x1000, max_fee=0x1,
                             sender=SENDER_B, nonce="0x9")
        cheap_gas, cheap_cost = request_profile(cheap)
        doc = document(
            [pricey, cheap],
            bundle_gas=ITEM_GAS + cheap_gas,
            bundle_cost=cheap_cost,
        )
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"],
                         [{"index": 0, "reason": "E_BUNDLE_BUDGET"}])

    def test_mixed_conflict_and_limits(self):
        # 0:K1、1:K2 入选；2 是 K1 的重复 → 冲突；3:K3 为大请求，替换或并入都
        # 使 gas 超限 → E_BUNDLE_GAS。
        big_gas_req = make_request(call_gas=0x20000, sender=SENDER_C, nonce="0x3")
        big_gas, _ = request_profile(big_gas_req)
        reqs = [
            make_request(sender=SENDER_A, nonce="0x1"),
            make_request(sender=SENDER_B, nonce="0x2"),
            make_request(sender=SENDER_A, nonce="0x1"),
            big_gas_req,
        ]
        doc = document(
            reqs,
            bundle_gas=ITEM_GAS * 2,  # 两个标准请求恰好放下，加大请求必超
            bundle_cost=ITEM_COST * 4,
        )
        self.assertGreater(ITEM_GAS + big_gas, ITEM_GAS * 2)
        plan = plan_of(plan_bundle_nonce_unique(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 2, "reason": "E_NONCE_CONFLICT"},
                {"index": 3, "reason": "E_BUNDLE_GAS"},
            ],
        )
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))

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
        plan = plan_of(plan_bundle_nonce_unique(doc))
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
            plan_bundle_nonce_unique(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_gas_checked_before_budget_for_excluded(self):
        plan = plan_of(
            plan_bundle_nonce_unique(
                document([make_request()], bundle_gas=ITEM_GAS - 1,
                         bundle_cost=ITEM_COST - 1)
            )
        )
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_selected_and_skipped_sorted_and_strings_decimal(self):
        reqs = [make_request(sender=SENDER_A, nonce="0x0"),
                make_request(sender=SENDER_A, nonce="0x1"),
                make_request(sender=SENDER_A, nonce="0x1"),
                make_request(sender=SENDER_A, nonce="0x1")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 3, bundle_cost=ITEM_COST * 3)
        plan = plan_of(plan_bundle_nonce_unique(doc))
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
        plan_bundle_nonce_unique(doc)
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
                    nonce=hex(rng.randint(0, 2)),
                )
                reqs.append(req)
                profiles.append(request_profile(req))
                keys.append(
                    (
                        req["userOperation"]["sender"].lower(),
                        int(req["userOperation"]["nonce"], 16),
                    )
                )
            max_gas = rng.randint(ITEM_GAS // 2, ITEM_GAS * 3)
            max_cost = rng.randint(ITEM_COST // 2, ITEM_COST * 3)
            doc = document(reqs, bundle_gas=max_gas, bundle_cost=max_cost)
            plan = plan_of(plan_bundle_nonce_unique(doc))

            selected = plan["selected"]
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            selected_keys = [keys[i] for i in selected]
            self.assertEqual(len(set(selected_keys)), len(selected_keys))

            # 与穷举最优（数量、gas、cost）一致。
            best_key = None
            for mask in range(1 << count):
                chosen_idx = [i for i in range(count) if mask >> i & 1]
                chosen_keys = [keys[i] for i in chosen_idx]
                if len(set(chosen_keys)) != len(chosen_keys):
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

            chosen_key_set = set(selected_keys)
            for item in plan["skipped"]:
                if item["reason"] in ("E_GAS_LIMIT", "E_BUDGET"):
                    continue
                index = item["index"]
                gas, cost = profiles[index]
                if keys[index] in chosen_key_set:
                    self.assertEqual(item["reason"], "E_NONCE_CONFLICT", (doc, item))
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
                err = error_of(plan_bundle_nonce_unique(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(
            plan_bundle_nonce_unique({"requests": [], "extra": 1})
        )
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
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["nonce"]
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_MISSING_FIELD, "/requests/1/userOperation/nonce"))

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
        doc["bundlePolicy"]["maxCostWei"] = "0x0"
        err = error_of(plan_bundle_nonce_unique(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/bundlePolicy/maxCostWei"))

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
        reqs = [
            make_request(sender=SENDER_A, nonce="0x0"),
            make_request(sender=SENDER_B, nonce="0x0"),
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

    def test_conflict_exit_0(self):
        reqs = [make_request(nonce="0x0"), make_request(nonce="0x0")]
        doc = document(reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2)
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])
        self.assertEqual(result["plan"]["skipped"],
                         [{"index": 1, "reason": "E_NONCE_CONFLICT"}])

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
