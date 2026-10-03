"""paymaster.plan_bundle_max_count 与 ``python -m paymaster.pack_max_count`` 的测试。

运行：python -m unittest discover -s tests -v
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

from paymaster.packing import plan_bundle, plan_bundle_max_count  # noqa: E402
from paymaster.validation import E_INVALID_JSON  # noqa: E402

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"

# gas 单位放大到足以拆出 callGasLimit（verification+preVerification 固定 0x50000）。
G = 500_000
_FIXED_GAS = 0x20000 + 0x30000


def make_request(gas: int, fee: int = 1) -> dict:
    """构造 totalGas 恰为 gas、maxFeePerGas 为 fee 的合法请求（cost=gas*fee）。"""
    assert gas > _FIXED_GAS
    return {
        "userOperation": {
            "sender": SENDER,
            "nonce": "0x0",
            "callData": "0x",
            "callGasLimit": hex(gas - _FIXED_GAS),
            "verificationGasLimit": "0x20000",
            "preVerificationGas": "0x30000",
            "maxFeePerGas": hex(fee),
            "maxPriorityFeePerGas": hex(fee),
            "signature": "0x",
        },
        "context": {
            "version": "0.7",
            "chainId": "0x1",
            "entryPoint": ENTRY_POINT,
            "baseFeePerGas": "0x7",
        },
    }


def document(items: list[tuple[int, int]], gas_cap: int, cost_cap: int) -> dict:
    """items 为 (gas, fee) 列表；另加两个超 sponsorship 限额的请求由用例自定。"""
    return {
        "requests": [make_request(gas, fee) for gas, fee in items],
        "sponsorshipPolicy": {
            "budgetWei": hex(10**20),
            "maxTotalGas": hex(10**10),
        },
        "bundlePolicy": {
            "maxTotalGas": hex(gas_cap),
            "maxCostWei": hex(cost_cap),
        },
    }


def plan_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result["plan"]


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


class TestSelection(unittest.TestCase):
    def test_all_fit(self):
        plan = plan_of(plan_bundle_max_count(document(
            [(4 * G, 1), (4 * G, 1), (4 * G, 1)], 100 * G, 100 * G
        )))
        self.assertEqual(plan["selected"], [0, 1, 2])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "3")
        self.assertEqual(plan["totalGas"], str(12 * G))
        self.assertEqual(plan["estimatedCostWei"], str(12 * G))

    def test_empty_requests_succeeds(self):
        plan = plan_of(plan_bundle_max_count(document([], 100 * G, 100 * G)))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

    def test_prefers_more_items_over_sequential_greedy(self):
        # 顺序贪心只会选下标 0（6G 占满 gas），最大数量应选 [1, 2]。
        doc = document(
            [(6 * G, 1), (4 * G, 1), (4 * G, 1)], 8 * G, 8 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])
        self.assertEqual(plan["totalGas"], str(8 * G))

    def test_prefers_more_items_under_budget_constraint(self):
        # 顺序贪心选 0（小 gas、高费用）后占满预算只得 1 项；
        # 最大数量应选两个便宜请求 [1, 2]，且 0 加入后超成本。
        doc = document(
            [(1 * G, 8), (4 * G, 1), (4 * G, 1)], 10 * G, 8 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(
            plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_BUDGET"}]
        )
        self.assertEqual(plan["estimatedCostWei"], str(8 * G))

    def test_tie_broken_by_smaller_total_gas(self):
        # 三个 2 项方案中 {0,2} 与 {1,2} 总 gas(6G) 小于 {0,1}(8G)。
        doc = document(
            [(4 * G, 1), (4 * G, 1), (2 * G, 1)], 8 * G, 100 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_GAS"}])

    def test_tie_broken_by_smaller_total_cost(self):
        # {0,2} 与 {1,2} 总 gas 相同，{1,2} 总成本更小。
        doc = document(
            [(4 * G, 2), (4 * G, 1), (2 * G, 1)], 6 * G, 100 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [1, 2])
        self.assertEqual(plan["estimatedCostWei"], str(6 * G))

    def test_tie_broken_by_index_sequence_lexicographic(self):
        doc = document(
            [(4 * G, 1), (4 * G, 1), (4 * G, 1)], 8 * G, 8 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_BUNDLE_GAS"}])

    def test_skipped_gas_reason_when_adding_exceeds_gas_first(self):
        doc = document(
            [(4 * G, 1), (4 * G, 1), (6 * G, 1)], 8 * G, 20 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["skipped"], [{"index": 2, "reason": "E_BUNDLE_GAS"}])

    def test_skipped_budget_reason_when_gas_fits_but_cost_exceeds(self):
        doc = document(
            [(4 * G, 1), (4 * G, 1), (1 * G, 1)], 20 * G, 8 * G
        )
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0, 2])
        self.assertEqual(
            plan["skipped"], [{"index": 1, "reason": "E_BUNDLE_BUDGET"}]
        )

    def test_no_feasible_selection_still_succeeds(self):
        doc = document([(4 * G, 1)], 2 * G, 100 * G)
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_GAS"}])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")

        doc = document([(4 * G, 1)], 100 * G, 1 * G)
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [])
        self.assertEqual(
            plan["skipped"], [{"index": 0, "reason": "E_BUNDLE_BUDGET"}]
        )

    def test_unapproved_requests_keep_reasons_and_never_participate(self):
        # sponsorship 限额：maxTotalGas=10G（12G 项触发 E_GAS_LIMIT），
        # budgetWei=10^15（1G * 10^15 费触发 E_BUDGET，4G 正常项不受影响）。
        doc = {
            "requests": [
                make_request(4 * G, 1),
                make_request(12 * G, 1),
                make_request(1 * G, 10**15),
            ],
            "sponsorshipPolicy": {
                "budgetWei": hex(10**15),
                "maxTotalGas": hex(10 * G),
            },
            "bundlePolicy": {
                "maxTotalGas": hex(100 * G),
                "maxCostWei": hex(10**20),
            },
        }
        plan = plan_of(plan_bundle_max_count(doc))
        self.assertEqual(plan["selected"], [0])
        self.assertEqual(
            plan["skipped"],
            [
                {"index": 1, "reason": "E_GAS_LIMIT"},
                {"index": 2, "reason": "E_BUDGET"},
            ],
        )

    def test_summary_values_are_decimal_strings(self):
        plan = plan_of(plan_bundle_max_count(document(
            [(4 * G, 1), (4 * G, 1)], 100 * G, 100 * G
        )))
        for key in ("operationCount", "totalGas", "estimatedCostWei"):
            self.assertIsInstance(plan[key], str)
            self.assertRegex(plan[key], r"^[0-9]+$")

    def test_inputs_not_mutated(self):
        doc = document(
            [(6 * G, 1), (4 * G, 1), (4 * G, 1)], 8 * G, 8 * G
        )
        doc_copy = copy.deepcopy(doc)
        plan_bundle_max_count(doc)
        self.assertEqual(doc, doc_copy)


class TestOptimalByBruteForce(unittest.TestCase):
    """对随机小实例做全集枚举，校验数量与三级平局规则及 skip 原因。"""

    @staticmethod
    def _brute_force(items, gas_cap, cost_cap):
        n = len(items)
        best = None
        for mask in range(1 << n):
            indices = [i for i in range(n) if mask & (1 << i)]
            gas = sum(items[i][0] for i in indices)
            cost = sum(items[i][0] * items[i][1] for i in indices)
            if gas <= gas_cap and cost <= cost_cap:
                # 数量最大化，其后总 gas、总成本、下标序列最小化。
                key = (-len(indices), gas, cost, tuple(indices))
                if best is None or key < best[0]:
                    best = (key, indices, gas, cost)
        return best

    def test_random_instances_match_brute_force(self):
        rng = random.Random(20261004)
        for _ in range(300):
            n = rng.randint(0, 6)
            items = [
                (rng.randint(1, 6) * G, rng.randint(1, 4)) for _ in range(n)
            ]
            gas_cap = rng.randint(1, 12) * G
            cost_cap = rng.randint(1, 48) * G
            doc = document(items, gas_cap, cost_cap)
            plan = plan_of(plan_bundle_max_count(doc))

            best = self._brute_force(items, gas_cap, cost_cap)
            self.assertIsNotNone(best, f"empty subset must always fit: {items}")
            _, expected, exp_gas, exp_cost = best
            self.assertEqual(plan["selected"], expected)
            self.assertEqual(plan["totalGas"], str(exp_gas))
            self.assertEqual(plan["estimatedCostWei"], str(exp_cost))

            chosen = set(expected)
            reasons = {entry["index"]: entry["reason"] for entry in plan["skipped"]}
            self.assertEqual(set(reasons), set(range(n)) - chosen)
            for i in range(n):
                if i in chosen:
                    continue
                gas_over = exp_gas + items[i][0] > gas_cap
                cost_over = exp_cost + items[i][0] * items[i][1] > cost_cap
                self.assertTrue(gas_over or cost_over)
                self.assertEqual(
                    reasons[i],
                    "E_BUNDLE_GAS" if gas_over else "E_BUNDLE_BUDGET",
                )

            # selected 升序、skipped 按 index 排序。
            self.assertEqual(plan["selected"], sorted(plan["selected"]))
            skipped_indices = [entry["index"] for entry in plan["skipped"]]
            self.assertEqual(skipped_indices, sorted(skipped_indices))


class TestStructuralParity(unittest.TestCase):
    """结构校验必须与 plan_bundle 完全一致（code/path/message）。"""

    def _docs(self):
        base = document([(4 * G, 1), (4 * G, 1)], 100 * G, 100 * G)
        docs = [
            [], "x", 1, None, True,
            {}, {"requests": [], "extra": 1}, {"requests": []},
        ]
        unknown_key = copy.deepcopy(base)
        unknown_key["extra"] = {}
        docs.append(unknown_key)
        requests_not_array = copy.deepcopy(base)
        requests_not_array["requests"] = {}
        docs.append(requests_not_array)
        bad_request = copy.deepcopy(base)
        del bad_request["requests"][1]["userOperation"]["signature"]
        docs.append(bad_request)
        root_request_bad = copy.deepcopy(base)
        root_request_bad["requests"][0] = []
        docs.append(root_request_bad)
        for mutation in (
            lambda d: d.update(sponsorshipPolicy=[]),
            lambda d: d["sponsorshipPolicy"].pop("budgetWei"),
            lambda d: d["sponsorshipPolicy"].update(extra="0x1"),
            lambda d: d["sponsorshipPolicy"].update(maxTotalGas="0x0"),
            lambda d: d.update(bundlePolicy="x"),
            lambda d: d.update(bundlePolicy={}),
            lambda d: d["bundlePolicy"].update(budgetWei="0x1"),
            lambda d: d.update(
                bundlePolicy={"maxTotalGas": "0x0", "maxCostWei": "nope"}
            ),
        ):
            d = copy.deepcopy(base)
            mutation(d)
            docs.append(d)
        return docs

    def test_errors_identical_to_plan_bundle(self):
        for doc in self._docs():
            with self.subTest(document=doc):
                expected = plan_bundle(doc)
                actual = plan_bundle_max_count(doc)
                self.assertFalse(actual["ok"])
                self.assertEqual(actual, expected)
                self.assertEqual(set(actual["error"]), {"code", "path", "message"})


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.pack_max_count"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout

    def test_success_exit_0_single_json_doc(self):
        doc = document(
            [(6 * G, 1), (4 * G, 1), (4 * G, 1)], 8 * G, 8 * G
        )
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        docs = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(docs), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [1, 2])
        self.assertEqual(set(result), {"ok", "plan"})

    def test_empty_requests_exit_0(self):
        code, out = self.run_cli(json.dumps(document([], 100 * G, 100 * G)))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["plan"]["selected"], [])

    def test_structural_failure_exit_1(self):
        doc = document([(4 * G, 1)], 100 * G, 100 * G)
        del doc["requests"][0]["userOperation"]["sender"]
        code, out = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(out))

    def test_malformed_json_exit_1_invalid_json_empty_path(self):
        code, out = self.run_cli("{not json")
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_INVALID_JSON)
        self.assertEqual(error["path"], "")

    def test_non_object_document_exit_1(self):
        code, out = self.run_cli("[1, 2]")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], E_INVALID_JSON)


if __name__ == "__main__":
    unittest.main()
