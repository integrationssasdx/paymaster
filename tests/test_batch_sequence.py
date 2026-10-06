"""paymaster.plan_batch_sequence 与其 CLI 的单元测试。

运行：python -m unittest tests.test_batch_sequence -v
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
from paymaster.batch_sequence import (  # noqa: E402
    E_BATCH_POLICY_INVALID_FIELD,
    E_BATCH_POLICY_MISSING_FIELD,
    E_BATCH_POLICY_UNKNOWN_FIELD,
    REASON_NOT_SELECTED,
    plan_batch_sequence,
)
from paymaster.packing import (  # noqa: E402
    E_NONCE_STATE_INVALID_FIELD,
    E_USAGE_INVALID_FIELD,
)
from paymaster.validation import E_INVALID_JSON, E_UNKNOWN_FIELD  # noqa: E402

from tests.test_batch_state import (  # noqa: E402
    ITEM_COST,
    ITEM_GAS,
    NONCE_MOD,
    SENDER_A,
    SENDER_B,
    SENDER_C,
    document as base_document,
    make_request,
    nonce_of,
    state_entry,
    usage_entry,
)


def document(
    requests,
    *,
    max_batch_count: int = 4,
    max_operations_per_batch: int = 16,
    **kwargs,
):
    doc = base_document(requests, **kwargs)
    doc["batchPolicy"] = {
        "maxBatchCount": hex(max_batch_count),
        "maxOperationsPerBatch": hex(max_operations_per_batch),
    }
    return doc


def result_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


class TestSuccessShape(unittest.TestCase):
    def test_empty_requests(self):
        got = result_of(
            plan_batch_sequence(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(
            set(got),
            {"ok", "plan", "nextSenderUsage", "nextSenderNonceState"},
        )
        plan = got["plan"]
        self.assertEqual(
            set(plan),
            {
                "selected",
                "skipped",
                "operationCount",
                "totalGas",
                "estimatedCostWei",
                "batches",
            },
        )
        self.assertEqual(plan["selected"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["batches"], [])
        self.assertEqual(plan["operationCount"], "0")
        self.assertEqual(plan["totalGas"], "0")
        self.assertEqual(plan["estimatedCostWei"], "0")
        self.assertEqual(got["nextSenderUsage"], {})
        self.assertEqual(got["nextSenderNonceState"], {})

    def test_single_batch_summary(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 4,
                    bundle_cost=ITEM_COST * 4,
                )
            )
        )
        plan = got["plan"]
        self.assertEqual(plan["selected"], [0, 1])
        self.assertEqual(plan["operationCount"], "2")
        self.assertEqual(plan["totalGas"], str(ITEM_GAS * 2))
        self.assertEqual(plan["estimatedCostWei"], str(ITEM_COST * 2))
        self.assertEqual(
            plan["batches"],
            [
                {
                    "index": 0,
                    "selected": [0, 1],
                    "operationCount": "2",
                    "totalGas": str(ITEM_GAS * 2),
                    "estimatedCostWei": str(ITEM_COST * 2),
                }
            ],
        )

    def test_split_by_operations_per_batch(self):
        reqs = [
            make_request(nonce=nonce_of(0, seq)) for seq in range(3)
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                    max_operations_per_batch=1,
                    max_gas_per_sender=ITEM_GAS * 10,
                    max_cost_per_sender=ITEM_COST * 10,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            [b["selected"] for b in got["plan"]["batches"]], [[0], [1], [2]]
        )
        self.assertEqual([b["index"] for b in got["plan"]["batches"]], [0, 1, 2])
        for batch in got["plan"]["batches"]:
            self.assertEqual(batch["operationCount"], "1")
            self.assertEqual(batch["totalGas"], str(ITEM_GAS))
            self.assertEqual(batch["estimatedCostWei"], str(ITEM_COST))

    def test_split_by_bundle_limits(self):
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
                    max_batch_count=3,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])
        self.assertEqual(
            [b["selected"] for b in got["plan"]["batches"]], [[0], [1]]
        )

    def test_batch_count_cap_leaves_not_selected(self):
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
                    max_batch_count=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0])
        self.assertEqual(
            got["plan"]["skipped"],
            [{"index": 1, "reason": REASON_NOT_SELECTED}],
        )
        self.assertEqual(
            [b["selected"] for b in got["plan"]["batches"]], [[0]]
        )

    def test_batches_are_nonempty_and_partition_selected(self):
        reqs = [
            make_request(nonce=nonce_of(0, seq)) for seq in range(5)
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_operations_per_batch=2,
                    max_gas_per_sender=ITEM_GAS * 10,
                    max_cost_per_sender=ITEM_COST * 10,
                )
            )
        )
        batches = got["plan"]["batches"]
        self.assertTrue(all(b["selected"] for b in batches))
        flat = [index for batch in batches for index in batch["selected"]]
        self.assertEqual(flat, got["plan"]["selected"])
        self.assertEqual(len(flat), len(set(flat)))
        # 批内汇总之和等于顶层汇总。
        self.assertEqual(
            str(sum(int(b["totalGas"]) for b in batches)),
            got["plan"]["totalGas"],
        )
        self.assertEqual(
            str(sum(int(b["estimatedCostWei"]) for b in batches)),
            got["plan"]["estimatedCostWei"],
        )


class TestNonceContinuity(unittest.TestCase):
    def test_nonce_chain_continues_across_batches(self):
        # 每批只能放一项；seq0、seq1、seq2 必须跨批连续，状态推进到 2。
        reqs = [
            make_request(nonce=nonce_of(0, seq)) for seq in range(3)
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 10,
                    bundle_cost=ITEM_COST * 10,
                    max_operations_per_batch=1,
                    max_gas_per_sender=ITEM_GAS * 10,
                    max_cost_per_sender=ITEM_COST * 10,
                    nonce_state={SENDER_A: []},
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            got["nextSenderNonceState"],
            {SENDER_A: [{"nonceKey": "0", "lastSequence": "2"}]},
        )

    def test_anchored_chain_across_batches(self):
        reqs = [
            make_request(nonce=nonce_of(0, 5)),
            make_request(nonce=nonce_of(0, 6)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 4,
                    bundle_cost=ITEM_COST * 4,
                    max_operations_per_batch=1,
                    nonce_state={SENDER_A: [state_entry(0, 4)]},
                )
            )
        )
        self.assertEqual(
            [b["selected"] for b in got["plan"]["batches"]], [[0], [1]]
        )
        self.assertEqual(
            got["nextSenderNonceState"][SENDER_A],
            [{"nonceKey": "0", "lastSequence": "6"}],
        )

    def test_gap_cannot_be_filled_by_new_batch(self):
        # 新批不重置 nonce：seq2 无法接在 seq0 之后，即使允许另起一批。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 2)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batch_count=3,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0])
        self.assertEqual(
            got["plan"]["skipped"],
            [{"index": 1, "reason": REASON_NOT_SELECTED}],
        )

    def test_chain_resumes_by_index_order_not_value_order(self):
        # 下标顺序 seq0、seq2、seq1：seq2 跳过，seq1 仍可接续入选。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 2)),
            make_request(nonce=nonce_of(0, 1)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 2])
        self.assertEqual(
            got["plan"]["skipped"],
            [{"index": 1, "reason": REASON_NOT_SELECTED}],
        )
        self.assertEqual(
            got["nextSenderNonceState"][SENDER_A],
            [{"nonceKey": "0", "lastSequence": "1"}],
        )


class TestSkippedReasons(unittest.TestCase):
    def test_unsponsored_requests_keep_reason(self):
        rejected_gas = make_request(nonce=nonce_of(0, 0))
        rejected_gas["userOperation"]["preVerificationGas"] = "0x200000"
        # 高费率请求：totalGas 未超 sponsorship 限额，但估算成本超过预算。
        rejected_budget = make_request(
            max_fee=10**12, nonce=nonce_of(1, 0)
        )
        approved = make_request(sender=SENDER_B, nonce=nonce_of(0, 0))
        reqs = [rejected_gas, rejected_budget, approved]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3 + 10**12 * ITEM_GAS,
                    sponsorship_budget=10**10,
                )
            )
        )
        reasons = {item["index"]: item["reason"] for item in got["plan"]["skipped"]}
        self.assertEqual(reasons[0], "E_GAS_LIMIT")
        self.assertEqual(reasons[1], "E_BUDGET")
        self.assertEqual(got["plan"]["selected"], [2])

    def test_skipped_sorted_by_index(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(nonce=nonce_of(0, 2)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batch_count=1,
                )
            )
        )
        indexes = [item["index"] for item in got["plan"]["skipped"]]
        self.assertEqual(indexes, sorted(indexes))
        self.assertTrue(
            all(item["reason"] == REASON_NOT_SELECTED for item in got["plan"]["skipped"])
        )


class TestOptimizationPriorities(unittest.TestCase):
    def test_maximize_count_first(self):
        # bundle 只容一项、只许一批：只能选一个。
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
                    max_batch_count=1,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0])
        self.assertEqual(
            got["plan"]["skipped"],
            [{"index": 1, "reason": REASON_NOT_SELECTED}],
        )

    def test_minimize_batch_count(self):
        # 三项可同批容纳时不主动拆批。
        reqs = [
            make_request(nonce=nonce_of(0, seq)) for seq in range(3)
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 5,
                    bundle_cost=ITEM_COST * 5,
                    max_operations_per_batch=5,
                    max_gas_per_sender=ITEM_GAS * 5,
                    max_cost_per_sender=ITEM_COST * 5,
                )
            )
        )
        self.assertEqual(len(got["plan"]["batches"]), 1)
        self.assertEqual(got["plan"]["selected"], [0, 1, 2])

    def test_maximize_distinct_senders(self):
        # 单批只容两项、数量同为 2 时，覆盖两个 sender 的方案优先于同一
        # sender 的两项。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    max_batch_count=1,
                    max_operations_per_batch=2,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 2])
        self.assertEqual(
            got["plan"]["skipped"],
            [{"index": 1, "reason": REASON_NOT_SELECTED}],
        )

    def test_sender_budget_spans_all_batches(self):
        # sender 预算只够两项：即便允许多批也不能选三项。
        reqs = [
            make_request(nonce=nonce_of(0, seq)) for seq in range(3)
        ]
        got = result_of(
            plan_batch_sequence(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batch_count=3,
                    max_operations_per_batch=1,
                    max_gas_per_sender=ITEM_GAS * 2,
                    max_cost_per_sender=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])
        self.assertEqual(
            got["plan"]["skipped"],
            [{"index": 2, "reason": REASON_NOT_SELECTED}],
        )


class TestCarryForward(unittest.TestCase):
    def test_usage_accumulates_across_batches(self):
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
                    max_batch_count=3,
                    max_operations_per_batch=1,
                    usage={SENDER_A: usage_entry(100, 7)},
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

    def test_input_senders_preserved_even_when_not_selected(self):
        rejected = make_request(sender=SENDER_A, nonce=nonce_of(0, 0))
        rejected["userOperation"]["preVerificationGas"] = "0x200000"
        got = result_of(
            plan_batch_sequence(
                document(
                    [rejected],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={
                        SENDER_C: usage_entry(5, 9),
                        SENDER_A: usage_entry(3, 4),
                    },
                )
            )
        )
        self.assertEqual(
            got["nextSenderUsage"],
            {
                SENDER_A: {"totalGas": "3", "estimatedCostWei": "4"},
                SENDER_C: {"totalGas": "5", "estimatedCostWei": "9"},
            },
        )
        self.assertEqual(got["nextSenderNonceState"], {})

    def test_unselected_approved_sender_not_added(self):
        got = result_of(
            plan_batch_sequence(
                document(
                    [
                        make_request(nonce=nonce_of(0, 0)),
                        make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
                    ],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    max_batch_count=1,
                )
            )
        )
        self.assertEqual(set(got["nextSenderUsage"]), {SENDER_A})
        self.assertEqual(set(got["nextSenderNonceState"]), {SENDER_A})

    def test_state_entries_preserved_and_sorted(self):
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
                    max_batch_count=3,
                    max_operations_per_batch=1,
                    nonce_state={
                        SENDER_C: [state_entry(1, 2)],
                        SENDER_A: [
                            state_entry(5, 9),
                            state_entry(0, 100),  # 旧锚点阻止 seq0 入选
                        ],
                    },
                )
            )
        )
        # SENDER_A 的 seq0 不高于旧 lastSequence，全部未入选；状态原样保留
        # 并按 sender 与 nonceKey 数值升序规范化。
        self.assertEqual(got["plan"]["selected"], [1])
        self.assertEqual(
            got["nextSenderNonceState"],
            {
                SENDER_A: [
                    {"nonceKey": "0", "lastSequence": "100"},
                    {"nonceKey": "5", "lastSequence": "9"},
                ],
                SENDER_B: [{"nonceKey": "0", "lastSequence": "0"}],
                SENDER_C: [{"nonceKey": "1", "lastSequence": "2"}],
            },
        )


class TestBatchPolicyValidation(unittest.TestCase):
    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def test_policy_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(batchPolicy=bad):
                doc = self.valid_doc()
                doc["batchPolicy"] = bad
                err = error_of(plan_batch_sequence(doc))
                self.assertEqual(err["code"], E_BATCH_POLICY_INVALID_FIELD)
                self.assertEqual(err["path"], "/batchPolicy")

    def test_missing_fields(self):
        doc = self.valid_doc()
        del doc["batchPolicy"]["maxBatchCount"]
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/batchPolicy/maxBatchCount")

        doc = self.valid_doc()
        del doc["batchPolicy"]["maxOperationsPerBatch"]
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/batchPolicy/maxOperationsPerBatch")

    def test_missing_field_reported_before_unknown(self):
        doc = self.valid_doc()
        del doc["batchPolicy"]["maxBatchCount"]
        doc["batchPolicy"]["extra"] = "0x1"
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_BATCH_POLICY_MISSING_FIELD)
        self.assertEqual(err["path"], "/batchPolicy/maxBatchCount")

    def test_unknown_field(self):
        doc = self.valid_doc()
        doc["batchPolicy"]["extra"] = "0x1"
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_BATCH_POLICY_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/batchPolicy/extra")

    def test_invalid_values(self):
        bad_values = [
            "0x0",  # 必须为正
            "0x01",  # 多余前导零
            "1",  # 缺 0x 前缀
            "0x",  # 空
            1,  # 非字符串
            None,
            "0x10000000000000000",  # 2**64，超过上界
            "0xgg",  # 非十六进制
        ]
        for bad in bad_values:
            for field in ("maxBatchCount", "maxOperationsPerBatch"):
                with self.subTest(field=field, value=bad):
                    doc = self.valid_doc()
                    doc["batchPolicy"][field] = bad
                    err = error_of(plan_batch_sequence(doc))
                    self.assertEqual(err["code"], E_BATCH_POLICY_INVALID_FIELD)
                    self.assertEqual(err["path"], f"/batchPolicy/{field}")

    def test_invalid_value_reported_in_field_order(self):
        doc = self.valid_doc()
        doc["batchPolicy"]["maxBatchCount"] = "0x0"
        doc["batchPolicy"]["maxOperationsPerBatch"] = "0x0"
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["path"], "/batchPolicy/maxBatchCount")

    def test_upper_bound_accepted(self):
        doc = self.valid_doc()
        doc["batchPolicy"] = {
            "maxBatchCount": "0xffffffffffffffff",
            "maxOperationsPerBatch": "0xffffffffffffffff",
        }
        got = plan_batch_sequence(doc)
        self.assertTrue(got["ok"], got)


class TestStructuralParity(unittest.TestCase):
    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                err = error_of(plan_batch_sequence(bad))
                self.assertEqual(err["code"], E_INVALID_JSON)
                self.assertEqual(err["path"], "")

    def test_unknown_root_field(self):
        doc = document([], bundle_gas=1, bundle_cost=1)
        doc["extra"] = 1
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")

    def test_missing_root_fields_in_order(self):
        doc = document([], bundle_gas=1, bundle_cost=1)
        del doc["batchPolicy"]
        err = error_of(plan_batch_sequence(doc))
        # 前六字段存在时，缺第七个字段报根缺键。
        self.assertEqual(err["code"], "E_MISSING_FIELD")
        self.assertEqual(err["path"], "/batchPolicy")

        err = error_of(plan_batch_sequence({}))
        self.assertEqual(err["code"], "E_MISSING_FIELD")
        self.assertEqual(err["path"], "/requests")

    def test_request_error_path_prefixed(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        del doc["requests"][0]["userOperation"]["signature"]
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], "E_MISSING_FIELD")
        self.assertEqual(err["path"], "/requests/0/userOperation/signature")

    def test_batch_policy_validated_after_usage_and_state(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["senderUsage"] = []
        doc["batchPolicy"] = {}
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(err["path"], "/senderUsage")

        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["senderNonceState"] = []
        doc["batchPolicy"] = {}
        err = error_of(plan_batch_sequence(doc))
        self.assertEqual(err["code"], E_NONCE_STATE_INVALID_FIELD)
        self.assertEqual(err["path"], "/senderNonceState")

    def test_exported_from_package(self):
        self.assertIs(paymaster.plan_batch_sequence, plan_batch_sequence)
        self.assertIn("plan_batch_sequence", paymaster.__all__)


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
            max_batch_count=3,
            max_operations_per_batch=1,
            usage={SENDER_A: usage_entry(10, 20)},
            nonce_state={SENDER_A: [state_entry(0, 0)]},
        )
        snapshot = copy.deepcopy(doc)
        plan_batch_sequence(doc)
        self.assertEqual(doc, snapshot)


class TestRandomBruteForce(unittest.TestCase):
    """小规模随机实例：与独立暴力枚举核对入选集合、批次数与汇总。"""

    def brute_force(self, cands, usage, nstate, mgas, mcost, bgas, bcost, B, L):
        n = len(cands)
        best = None
        for mask in range(1 << n):
            sel = [i for i in range(n) if mask >> i & 1]
            per_sender_gas: dict[str, int] = {}
            per_sender_cost: dict[str, int] = {}
            groups: dict[tuple[str, int], list[int]] = {}
            senders = set()
            for p in sel:
                sender, key, seq, g, c = cands[p]
                per_sender_gas[sender] = per_sender_gas.get(sender, 0) + g
                per_sender_cost[sender] = per_sender_cost.get(sender, 0) + c
                groups.setdefault((sender, key), []).append(seq)
                senders.add(sender)
            feasible = True
            for (sender, key), seqs in groups.items():
                state = nstate.get(sender)
                anchor = state[key] + 1 if state and key in state else 0
                if seqs != list(range(anchor, anchor + len(seqs))):
                    feasible = False
                    break
            if not feasible:
                continue
            for sender, g in per_sender_gas.items():
                ug, uc = usage.get(sender, (0, 0))
                if ug + g > mgas or uc + per_sender_cost[sender] > mcost:
                    feasible = False
                    break
            if not feasible:
                continue
            # 按入选子序列贪心切批（容量内尽量延续）。
            batches = []
            cur = []
            cur_g = cur_c = 0
            for p in sel:
                g, c = cands[p][3], cands[p][4]
                # 单项自身超过 bundle 限额则任何批次都无法容纳。
                if g > bgas or c > bcost:
                    feasible = False
                    break
                if (
                    len(cur) >= L
                    or cur_g + g > bgas
                    or cur_c + c > bcost
                ):
                    batches.append(tuple(cur))
                    cur, cur_g, cur_c = [], 0, 0
                cur.append(p)
                cur_g += g
                cur_c += c
            if not feasible:
                continue
            if cur:
                batches.append(tuple(cur))
            if len(batches) > B:
                continue
            total_gas = sum(cands[p][3] for p in sel)
            total_cost = sum(cands[p][4] for p in sel)
            key = (-len(sel), len(batches), -len(senders), total_gas, total_cost,
                   tuple(sel))
            if best is None or key < best[0]:
                best = (key, tuple(batches), total_gas, total_cost)
        return best

    def test_random_instances(self):
        from paymaster.batch_sequence import _choose_batch_sequence

        rng = random.Random(20261008)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(300):
            count = rng.randint(0, 5)
            cands = []
            for _i in range(count):
                cands.append((
                    rng.choice(senders),
                    rng.randint(0, 1),
                    rng.randint(0, 3),
                    rng.choice([1, 2, 3]),
                    rng.choice([1, 2, 3]),
                ))
            usage = {
                sender: (rng.randint(0, 2), rng.randint(0, 2))
                for sender in senders
                if rng.random() < 0.4
            }
            nstate = {}
            for sender in senders:
                if rng.random() < 0.5:
                    continue
                nstate[sender] = {
                    key: rng.randint(0, 2)
                    for key in range(2)
                    if rng.random() < 0.4
                }
            kwargs = (
                rng.randint(1, 8),
                rng.randint(1, 8),
                rng.randint(1, 5),
                rng.randint(1, 5),
                rng.randint(1, 3),
                rng.randint(1, 3),
            )
            batches, total_gas, total_cost = _choose_batch_sequence(
                cands, usage, nstate, *kwargs
            )
            want = self.brute_force(cands, usage, nstate, *kwargs)
            got_flat = tuple(p for b in batches for p in b)
            want_flat = tuple(p for b in want[1] for p in b)
            self.assertEqual(
                (got_flat, len(batches), total_gas, total_cost),
                (want_flat, len(want[1]), want[2], want[3]),
            )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str):
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
            [
                make_request(nonce=nonce_of(0, 0)),
                make_request(nonce=nonce_of(0, 1)),
                make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
            ],
            bundle_gas=ITEM_GAS,
            bundle_cost=ITEM_COST,
            max_batch_count=3,
            max_operations_per_batch=1,
        )
        code, out, err = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            [b["selected"] for b in result["plan"]["batches"]],
            [[0], [1], [2]],
        )
        self.assertEqual(
            set(result),
            {"ok", "plan", "nextSenderUsage", "nextSenderNonceState"},
        )

    def test_batch_policy_error_exit_1(self):
        doc = document([make_request()], bundle_gas=1, bundle_cost=1)
        doc["batchPolicy"]["maxBatchCount"] = "0x0"
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], E_BATCH_POLICY_INVALID_FIELD)
        self.assertEqual(error["path"], "/batchPolicy/maxBatchCount")

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
