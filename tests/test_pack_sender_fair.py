"""paymaster.plan_bundle_sender_fair 与其 CLI 的单元测试。

运行：python -m unittest tests.test_pack_sender_fair -v
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

from paymaster.packing import _choose_sender_fair, plan_bundle_sender_fair  # noqa: E402
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


def make_request(sender: str = SENDER_A, call_gas: int = 0x10000,
                 max_fee: int = 0x10) -> dict:
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


def document(requests, *, bundle_gas: int, bundle_cost: int,
             max_per_sender: int = 1,
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


class TestChooseSenderFair(unittest.TestCase):
    """直接对选择核心做穷举交叉验证。"""

    def brute_force(self, groups, quota, max_gas, max_cost):
        flat = [
            (gas, cost, idx, sender_no)
            for sender_no, group in enumerate(groups)
            for gas, cost, idx in group
        ]
        best_key = None
        best = ((), 0, 0)
        for mask in range(1 << len(flat)):
            picked = [flat[i] for i in range(len(flat)) if mask >> i & 1]
            gas = sum(item[0] for item in picked)
            cost = sum(item[1] for item in picked)
            if gas > max_gas or cost > max_cost:
                continue
            counts = {}
            for item in picked:
                counts[item[3]] = counts.get(item[3], 0) + 1
            if any(count > quota for count in counts.values()):
                continue
            chosen = tuple(sorted(item[2] for item in picked))
            key = (-len(chosen), -len(counts), gas, cost, chosen)
            if best_key is None or key < best_key:
                best_key = key
                best = (chosen, gas, cost)
        return best

    def make_groups(self, rng, count, sender_pool):
        """生成 n 个候选，sender 随机；按 sender 首现分组，组内序号递增。"""
        senders = [rng.randrange(sender_pool) for _ in range(count)]
        groups = []
        by_sender = {}
        for idx, sender in enumerate(senders):
            group = by_sender.setdefault(sender, [])
            if not group:
                groups.append(group)
            group.append((rng.randint(1, 7), rng.randint(1, 40), idx))
        return groups

    def test_empty(self):
        self.assertEqual(_choose_sender_fair([], 1, 10, 10), ((), 0, 0))

    def test_quota_caps_single_sender(self):
        groups = [[(2, 2, 0), (2, 2, 1), (2, 2, 2)]]
        chosen, gas, cost = _choose_sender_fair(groups, 2, 100, 100)
        self.assertEqual((chosen, gas, cost), ((0, 1), 4, 4))

    def test_prefers_more_senders_on_count_tie(self):
        # 数量并列 2：{0,1} 只含 1 个 sender，{0,2}/{1,2} 含 2 个。
        groups = [[(2, 2, 0), (2, 2, 1)], [(2, 2, 2)]]
        chosen, _, _ = _choose_sender_fair(groups, 2, 4, 4)
        self.assertEqual(chosen, (0, 2))

    def test_gas_tiebreak_after_sender_count(self):
        # 数量与 sender 数都并列，取总 gas 较小者。
        groups = [[(3, 1, 0)], [(2, 9, 1)]]
        chosen, gas, cost = _choose_sender_fair(groups, 1, 3, 10)
        self.assertEqual((chosen, gas, cost), ((1,), 2, 9))

    def test_cost_tiebreak_after_gas(self):
        groups = [[(2, 5, 0)], [(2, 3, 1)]]
        chosen, gas, cost = _choose_sender_fair(groups, 1, 2, 10)
        self.assertEqual((chosen, gas, cost), ((1,), 2, 3))

    def test_lexicographic_final_tiebreak(self):
        groups = [[(2, 2, 0)], [(2, 2, 1)], [(2, 2, 2)]]
        chosen, _, _ = _choose_sender_fair(groups, 1, 4, 4)
        self.assertEqual(chosen, (0, 1))

    def test_none_fit(self):
        groups = [[(5, 5, 0), (6, 1, 1)]]
        self.assertEqual(_choose_sender_fair(groups, 2, 4, 100), ((), 0, 0))

    def test_random_instances_against_brute_force(self):
        rng = random.Random(20261004)
        for _ in range(2000):
            count = rng.randint(0, 9)
            groups = self.make_groups(rng, count, sender_pool=rng.randint(1, 4))
            quota = rng.randint(1, 3)
            max_gas = rng.randint(0, 25)
            max_cost = rng.randint(0, 140)
            got = _choose_sender_fair(groups, quota, max_gas, max_cost)
            want = self.brute_force(groups, quota, max_gas, max_cost)
            self.assertEqual(got, want, (groups, quota, max_gas, max_cost, got, want))


class TestPlanEndToEnd(unittest.TestCase):
    def test_all_selected_when_capacity_ample(self):
        plan = plan_of(
            plan_bundle_sender_fair(
                document(
                    [make_request(SENDER_A), make_request(SENDER_B)],
                    bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10,
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
            plan_bundle_sender_fair(document([], bundle_gas=1, bundle_cost=1))
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
            plan_bundle_sender_fair(
                document([req], bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10)
            )
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}])
        self.assertEqual(plan["operationCount"], "0")

    def test_same_sender_partial_selection_succeeds(self):
        reqs = [make_request(SENDER_A) for _ in range(3)]
        plan = plan_of(
            plan_bundle_sender_fair(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10, max_per_sender=2)
            )
        )
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_SENDER_QUOTA"}])
        self.assertEqual(plan["operationCount"], "2")

    def test_sender_addresses_grouped_case_insensitively(self):
        mixed = "0x" + "A" * 40
        lower = "0x" + "a" * 40
        reqs = [make_request(mixed), make_request(lower)]
        plan = plan_of(
            plan_bundle_sender_fair(
                document(reqs, bundle_gas=ITEM_GAS * 10,
                         bundle_cost=ITEM_COST * 10, max_per_sender=1)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_QUOTA"}])

    def test_quota_reason_before_bundle_limits(self):
        # 同 sender 两笔，配额 1；第二笔即使 gas 也超限，仍记 E_SENDER_QUOTA。
        reqs = [make_request(SENDER_A), make_request(SENDER_A)]
        plan = plan_of(
            plan_bundle_sender_fair(
                document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST,
                         max_per_sender=1)
            )
        )
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_SENDER_QUOTA"}])

    def test_distinct_senders_maximized_on_count_tie(self):
        # 限额恰容两笔：{0,1} 只有 1 个 sender，{0,2} 有 2 个 sender 胜出。
        reqs = [
            make_request(SENDER_A),
            make_request(SENDER_A),
            make_request(SENDER_B),
        ]
        plan = plan_of(
            plan_bundle_sender_fair(
                document(reqs, bundle_gas=ITEM_GAS * 2,
                         bundle_cost=ITEM_COST * 2, max_per_sender=2)
            )
        )
        self.assertEqual(plan["selected"], [0, 2])
        # 1 号 sender 未满（1/2），并入使 gas 超限。
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_gas_tiebreak_after_sender_count(self):
        # 只能容一笔：0 号 gas 大但便宜，1 号 gas 小但贵；gas 较小者胜。
        heavy = make_request(SENDER_A, call_gas=0x20000)  # gas 458752
        light = make_request(SENDER_B, call_gas=0x1000, max_fee=0x20)  # gas 331776
        heavy_gas, heavy_cost = request_profile(heavy)
        light_gas, light_cost = request_profile(light)
        doc = document(
            [heavy, light],
            bundle_gas=max(heavy_gas, light_gas),
            bundle_cost=max(heavy_cost, light_cost),
        )
        plan = plan_of(plan_bundle_sender_fair(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_cost_tiebreak_after_gas(self):
        # gas 相同、成本不同，只能容一笔。
        pricey = make_request(SENDER_A, call_gas=0x10000, max_fee=0x20)
        cheap = make_request(SENDER_B, call_gas=0x10000, max_fee=0x10)
        gas, pricey_cost = request_profile(pricey)
        _, cheap_cost = request_profile(cheap)
        doc = document(
            [pricey, cheap],
            bundle_gas=gas,
            bundle_cost=max(pricey_cost, cheap_cost),
        )
        plan = plan_of(plan_bundle_sender_fair(doc))
        self.assertEqual(plan["selected"], [1])
        # 0 号 sender 未满，并入使 gas 超限 -> E_BUNDLE_GAS
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])

    def test_budget_reason_when_gas_fits_but_cost_does_not(self):
        # 0 号便宜小请求入选；1 号（另一 sender）并入 gas 不超、成本超。
        cheap = make_request(SENDER_A, call_gas=0x1000, max_fee=0x1)
        pricey = make_request(SENDER_B, call_gas=0x10000, max_fee=0x100)
        cheap_gas, cheap_cost = request_profile(cheap)
        pricey_gas, _ = request_profile(pricey)
        doc = document(
            [cheap, pricey],
            bundle_gas=cheap_gas + pricey_gas,  # gas 够两笔
            bundle_cost=cheap_cost,  # 成本只够便宜的
        )
        plan = plan_of(plan_bundle_sender_fair(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_BUDGET"}])
        self.assertEqual(plan["estimatedCostWei"], str(cheap_cost))

    def test_lexicographic_final_tiebreak_end_to_end(self):
        reqs = [
            make_request(SENDER_A),
            make_request(SENDER_B),
            make_request(SENDER_C),
        ]
        doc = document(
            reqs, bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 3,
        )
        plan = plan_of(plan_bundle_sender_fair(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_BUNDLE_GAS"}])

    def test_unapproved_requests_keep_reason_and_position(self):
        gas_rejected = make_request(SENDER_A)
        gas_rejected["userOperation"]["preVerificationGas"] = "0x200000"
        budget_rejected = make_request(SENDER_B, max_fee=0xDE0B6B3A7640000)
        ok_req = make_request(SENDER_C)
        doc = document(
            [gas_rejected, ok_req, budget_rejected],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
        )
        plan = plan_of(plan_bundle_sender_fair(doc))
        self.assertEqual(plan["selected"], [1])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 0, "reason": "E_GAS_LIMIT"},
                {"index": 2, "reason": "E_BUDGET"},
            ],
        )

    def test_selected_and_skipped_sorted_and_strings_decimal(self):
        senders = [SENDER_A, SENDER_B, SENDER_C, SENDER_A]
        doc = document(
            [make_request(sender) for sender in senders],
            bundle_gas=ITEM_GAS * 2, bundle_cost=ITEM_COST * 2,
            max_per_sender=1,
        )
        plan = plan_of(plan_bundle_sender_fair(doc))
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
            [make_request(SENDER_A), make_request(SENDER_B)],
            bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST,
        )
        snapshot = copy.deepcopy(doc)
        plan_bundle_sender_fair(doc)
        self.assertEqual(doc, snapshot)

    def test_every_unselected_approved_violates_a_limit(self):
        """不变式：未入选的 approved 请求必然配额已满或并入后超限。"""
        rng = random.Random(7)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(50):
            count = rng.randint(1, 6)
            reqs = [
                make_request(
                    rng.choice(senders),
                    call_gas=rng.choice([0x1000, 0x4000, 0x10000, 0x20000]),
                    max_fee=rng.choice([0x1, 0x8, 0x10, 0x40]),
                )
                for _ in range(count)
            ]
            profiles = [request_profile(r) for r in reqs]
            quota = rng.randint(1, 3)
            doc = document(
                reqs,
                bundle_gas=rng.randint(ITEM_GAS // 2, ITEM_GAS * 3),
                bundle_cost=rng.randint(ITEM_COST // 2, ITEM_COST * 3),
                max_per_sender=quota,
            )
            plan = plan_of(plan_bundle_sender_fair(doc))
            selected = set(plan["selected"])
            total_gas = int(plan["totalGas"])
            total_cost = int(plan["estimatedCostWei"])
            max_gas = int(doc["bundlePolicy"]["maxTotalGas"], 16)
            max_cost = int(doc["bundlePolicy"]["maxCostWei"], 16)
            self.assertLessEqual(total_gas, max_gas)
            self.assertLessEqual(total_cost, max_cost)
            used = {}
            for index in selected:
                sender = reqs[index]["userOperation"]["sender"]
                used[sender] = used.get(sender, 0) + 1
            for count_per_sender in used.values():
                self.assertLessEqual(count_per_sender, quota)
            for index, (gas, cost) in enumerate(profiles):
                if index in selected:
                    continue
                sender = reqs[index]["userOperation"]["sender"]
                self.assertTrue(
                    used.get(sender, 0) >= quota
                    or total_gas + gas > max_gas
                    or total_cost + cost > max_cost,
                    (doc, plan, index),
                )


class TestStructureErrorsParity(unittest.TestCase):
    """结构错误码、path 与检查顺序与其余 plan_bundle_* 入口一致。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10,
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_bundle_sender_fair(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_key(self):
        doc = self.valid_doc()
        doc["extra"] = {}
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_unknown_checked_before_missing(self):
        err = error_of(plan_bundle_sender_fair({"requests": [], "extra": 1}))
        self.assertEqual((err["code"], err["path"]), (E_UNKNOWN_FIELD, "/extra"))

    def test_missing_key_order(self):
        err = error_of(plan_bundle_sender_fair({}))
        self.assertEqual((err["code"], err["path"]), (E_MISSING_FIELD, "/requests"))
        err = error_of(plan_bundle_sender_fair({"requests": []}))
        self.assertEqual(err["path"], "/sponsorshipPolicy")
        err = error_of(
            plan_bundle_sender_fair({"requests": [], "sponsorshipPolicy": {}})
        )
        self.assertEqual(err["path"], "/bundlePolicy")
        err = error_of(
            plan_bundle_sender_fair(
                {"requests": [], "sponsorshipPolicy": {}, "bundlePolicy": {}}
            )
        )
        self.assertEqual(err["path"], "/fairnessPolicy")

    def test_requests_not_array(self):
        for bad in ({}, "x", 1, None):
            with self.subTest(requests=bad):
                doc = self.valid_doc()
                doc["requests"] = bad
                err = error_of(plan_bundle_sender_fair(doc))
                self.assertEqual((err["code"], err["path"]),
                                 (E_INVALID_FIELD, "/requests"))

    def test_request_error_path_prefixed(self):
        doc = self.valid_doc()
        doc["requests"].append(make_request())
        del doc["requests"][1]["userOperation"]["signature"]
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_MISSING_FIELD, "/requests/1/userOperation/signature"))

    def test_request_root_error_path(self):
        doc = self.valid_doc()
        doc["requests"][0] = []
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_INVALID_JSON, "/requests/0"))

    def test_policy_errors(self):
        doc = self.valid_doc()
        doc["fairnessPolicy"] = {}
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_MISSING_FIELD, "/fairnessPolicy/maxPerSender"))

        doc = self.valid_doc()
        doc["fairnessPolicy"] = []
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/fairnessPolicy"))

        doc = self.valid_doc()
        doc["fairnessPolicy"]["maxPerSender"] = "0x0"
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/fairnessPolicy/maxPerSender"))

        doc = self.valid_doc()
        doc["fairnessPolicy"]["maxPerSender"] = 1
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_INVALID_FIELD, "/fairnessPolicy/maxPerSender"))

        doc = self.valid_doc()
        doc["fairnessPolicy"]["extra"] = "0x1"
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual((err["code"], err["path"]),
                         (E_POLICY_UNKNOWN_FIELD, "/fairnessPolicy/extra"))

    def test_policy_order_sponsorship_before_bundle_before_fairness(self):
        doc = self.valid_doc()
        doc["sponsorshipPolicy"] = []
        doc["bundlePolicy"] = []
        doc["fairnessPolicy"] = []
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual(err["path"], "/sponsorshipPolicy")

        doc = self.valid_doc()
        doc["bundlePolicy"] = []
        doc["fairnessPolicy"] = []
        err = error_of(plan_bundle_sender_fair(doc))
        self.assertEqual(err["path"], "/bundlePolicy")

    def test_result_shapes(self):
        result = plan_bundle_sender_fair({"requests": []})
        self.assertFalse(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "error"})
        self.assertEqual(set(result["error"].keys()), {"code", "path", "message"})

        result = plan_bundle_sender_fair(document([], bundle_gas=1, bundle_cost=1))
        self.assertTrue(result["ok"])
        self.assertEqual(set(result.keys()), {"ok", "plan"})
        self.assertEqual(
            set(result["plan"].keys()),
            {"selected", "skipped", "operationCount", "totalGas", "estimatedCostWei"},
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_sender_fair"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [make_request(SENDER_A), make_request(SENDER_B)],
            bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST * 2,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0])

    def test_quota_limited_selection_exit_0(self):
        doc = document(
            [make_request(SENDER_A), make_request(SENDER_A)],
            bundle_gas=ITEM_GAS * 10, bundle_cost=ITEM_COST * 10,
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
