"""paymaster.advance_batch_state 与其 CLI 的单元测试。

运行：python -m unittest tests.test_batch_state -v
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
from paymaster.batch_state import advance_batch_state  # noqa: E402
from paymaster.packing import (  # noqa: E402
    E_NONCE_STATE_INVALID_FIELD,
    E_USAGE_INVALID_FIELD,
    plan_bundle_sender_budget_with_nonce_state,
)
from paymaster.validation import (  # noqa: E402
    E_INVALID_JSON,
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


def result_of(result: dict) -> dict:
    assert result["ok"] is True, f"expected success, got {result}"
    return result


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


class TestPlanParity(unittest.TestCase):
    """plan 与现有规划入口的结果完全一致。"""

    def assert_plan_matches_planner(self, doc: dict):
        got = advance_batch_state(doc)
        want_plan = plan_bundle_sender_budget_with_nonce_state(doc)["plan"]
        self.assertEqual(got["ok"], True)
        self.assertEqual(got["plan"], want_plan)
        self.assertEqual(
            set(got.keys()),
            {"ok", "plan", "nextSenderUsage", "nextSenderNonceState"},
        )
        return got

    def test_empty_requests(self):
        got = self.assert_plan_matches_planner(
            document([], bundle_gas=1, bundle_cost=1)
        )
        self.assertEqual(got["plan"]["selected"], [])
        self.assertEqual(got["plan"]["operationCount"], "0")
        self.assertEqual(got["plan"]["totalGas"], "0")
        self.assertEqual(got["plan"]["estimatedCostWei"], "0")

    def test_chain_with_state(self):
        reqs = [
            make_request(nonce=nonce_of(0, 3)),
            make_request(nonce=nonce_of(0, 4)),
            make_request(nonce=nonce_of(0, 6)),
        ]
        got = self.assert_plan_matches_planner(
            document(
                reqs,
                bundle_gas=ITEM_GAS * 10,
                bundle_cost=ITEM_COST * 10,
                nonce_state={SENDER_A: [state_entry(0, 2)]},
            )
        )
        self.assertEqual(got["plan"]["selected"], [0, 1])
        self.assertEqual(
            got["plan"]["skipped"], [{"index": 2, "reason": "E_NONCE_GAP"}]
        )

    def test_multi_sender_with_skips(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        self.assert_plan_matches_planner(
            document(reqs, bundle_gas=ITEM_GAS, bundle_cost=ITEM_COST)
        )


class TestNextSenderUsage(unittest.TestCase):
    def test_empty_inputs(self):
        got = result_of(
            advance_batch_state(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(got["nextSenderUsage"], {})

    def test_selection_accumulates_per_sender(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
        ]
        got = result_of(
            advance_batch_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
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

    def test_input_senders_preserved_without_selection(self):
        # SENDER_C 在 senderUsage 中，但本批没有它的请求；SENDER_A 的请求被
        # 拒（approved=false），均不入选：两者的既有值原样保留。
        rejected = make_request(sender=SENDER_A, nonce=nonce_of(0, 0))
        rejected["userOperation"]["preVerificationGas"] = "0x200000"
        got = result_of(
            advance_batch_state(
                document(
                    [rejected, make_request(sender=SENDER_B, nonce=nonce_of(1, 1))],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={
                        SENDER_C: usage_entry(5, 9),
                        SENDER_A: usage_entry(3, 4),
                    },
                )
            )
        )
        # SENDER_B 的请求 sequence 1 在无状态组不能成链，无入选；不新增。
        self.assertEqual(
            got["nextSenderUsage"],
            {
                SENDER_A: {"totalGas": "3", "estimatedCostWei": "4"},
                SENDER_C: {"totalGas": "5", "estimatedCostWei": "9"},
            },
        )

    def test_unselected_request_sender_not_added(self):
        # SENDER_A 的请求因 bundle 限额落选且不在 senderUsage 中：不得新增。
        got = result_of(
            advance_batch_state(
                document(
                    [make_request(), make_request(sender=SENDER_B)],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                )
            )
        )
        self.assertEqual(set(got["nextSenderUsage"]), {SENDER_A})

    def test_values_are_canonical_decimal_strings(self):
        got = result_of(
            advance_batch_state(
                document(
                    [make_request(nonce=nonce_of(0, 0))],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                )
            )
        )
        entry = got["nextSenderUsage"][SENDER_A]
        for value in entry.values():
            self.assertIsInstance(value, str)
            self.assertRegex(value, r"^(?:0|[1-9][0-9]*)$")

    def test_sender_keys_sorted_and_unique(self):
        got = result_of(
            advance_batch_state(
                document(
                    [
                        make_request(nonce=nonce_of(0, 0)),
                        make_request(sender=SENDER_C, nonce=nonce_of(0, 0)),
                        make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
                    ],
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    usage={
                        SENDER_C: usage_entry(1, 1),
                        SENDER_A: usage_entry(2, 2),
                    },
                )
            )
        )
        keys = list(got["nextSenderUsage"])
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, [SENDER_A, SENDER_B, SENDER_C])

    def test_uppercase_input_key_normalized_and_merged_with_request(self):
        # senderUsage 的键与请求的 sender 均为非小写地址（按规范小写比较）；
        # 二者结转后必须合并为同一个规范小写键。
        mixed_sender = "0xAbCdEf0123456789abcdef0123456789ABCDEF01"
        req = make_request(sender=mixed_sender, nonce=nonce_of(0, 0))
        got = result_of(
            advance_batch_state(
                document(
                    [req],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    usage={mixed_sender: usage_entry(10, 20)},
                )
            )
        )
        self.assertEqual(
            got["nextSenderUsage"],
            {
                mixed_sender.lower(): {
                    "totalGas": str(10 + ITEM_GAS),
                    "estimatedCostWei": str(20 + ITEM_COST),
                }
            },
        )


class TestNextSenderNonceState(unittest.TestCase):
    def test_empty_inputs(self):
        got = result_of(
            advance_batch_state(document([], bundle_gas=1, bundle_cost=1))
        )
        self.assertEqual(got["nextSenderNonceState"], {})

    def test_stateful_chain_advances_to_max_sequence(self):
        reqs = [
            make_request(nonce=nonce_of(0, 3)),
            make_request(nonce=nonce_of(0, 4)),
        ]
        got = result_of(
            advance_batch_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 4,
                    bundle_cost=ITEM_COST * 4,
                    nonce_state={SENDER_A: [state_entry(0, 2)]},
                )
            )
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {SENDER_A: [{"nonceKey": "0", "lastSequence": "4"}]},
        )

    def test_stateless_selection_starts_at_zero(self):
        reqs = [
            make_request(nonce=nonce_of(7, 0)),
            make_request(nonce=nonce_of(7, 1)),
        ]
        got = result_of(
            advance_batch_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                )
            )
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {SENDER_A: [{"nonceKey": "7", "lastSequence": "1"}]},
        )

    def test_input_entries_preserved_when_key_or_sender_unselected(self):
        # SENDER_A 入选 key 0；其 key 5 与 SENDER_B 的 key 9 无入选，原样保留；
        # SENDER_C 只有空数组也保留。
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(nonce=nonce_of(0, 1)),
        ]
        got = result_of(
            advance_batch_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 4,
                    bundle_cost=ITEM_COST * 4,
                    nonce_state={
                        SENDER_A: [
                            state_entry(5, 11),
                            state_entry(0, 100),  # key 0 有旧锚点时请求 seq 0,1 不入选
                        ],
                        SENDER_B: [state_entry(9, 3)],
                        SENDER_C: [],
                    },
                )
            )
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {
                SENDER_A: [
                    {"nonceKey": "0", "lastSequence": "100"},
                    {"nonceKey": "5", "lastSequence": "11"},
                ],
                SENDER_B: [{"nonceKey": "9", "lastSequence": "3"}],
                SENDER_C: [],
            },
        )

    def test_new_sender_and_new_nonce_key_added(self):
        reqs = [
            make_request(nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
            make_request(sender=SENDER_B, nonce=nonce_of(0, 1)),
        ]
        got = result_of(
            advance_batch_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    nonce_state={
                        SENDER_A: [state_entry(3, 8)],
                        SENDER_C: [state_entry(1, 2)],
                    },
                )
            )
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {
                SENDER_A: [
                    {"nonceKey": "0", "lastSequence": "0"},
                    {"nonceKey": "3", "lastSequence": "8"},
                ],
                SENDER_B: [{"nonceKey": "0", "lastSequence": "1"}],
                SENDER_C: [{"nonceKey": "1", "lastSequence": "2"}],
            },
        )

    def test_nonce_keys_sorted_numerically(self):
        # 输入顺序为 10、2、100：必须按数值升序（而非字符串字典序）。
        reqs = [
            make_request(nonce=nonce_of(2, 0)),
            make_request(nonce=nonce_of(100, 0)),
            make_request(nonce=nonce_of(10, 0)),
        ]
        got = result_of(
            advance_batch_state(
                document(
                    reqs,
                    bundle_gas=ITEM_GAS * 3,
                    bundle_cost=ITEM_COST * 3,
                    nonce_state={SENDER_A: [state_entry(10, 5)]},
                )
            )
        )
        keys = [entry["nonceKey"] for entry in got["nextSenderNonceState"][SENDER_A]]
        self.assertEqual(keys, ["2", "10", "100"])
        self.assertEqual(
            got["nextSenderNonceState"][SENDER_A],
            [
                {"nonceKey": "2", "lastSequence": "0"},
                {"nonceKey": "10", "lastSequence": "5"},
                {"nonceKey": "100", "lastSequence": "0"},
            ],
        )

    def test_sender_keys_sorted(self):
        got = result_of(
            advance_batch_state(
                document(
                    [
                        make_request(sender=SENDER_C, nonce=nonce_of(0, 0)),
                        make_request(sender=SENDER_A, nonce=nonce_of(0, 0)),
                    ],
                    bundle_gas=ITEM_GAS * 2,
                    bundle_cost=ITEM_COST * 2,
                    nonce_state={SENDER_B: [state_entry(0, 1)]},
                )
            )
        )
        keys = list(got["nextSenderNonceState"])
        self.assertEqual(keys, [SENDER_A, SENDER_B, SENDER_C])

    def test_unselected_sender_not_added(self):
        # SENDER_A 因 gas 落选，且不在状态输入中：不新增。
        got = result_of(
            advance_batch_state(
                document(
                    [make_request(), make_request(sender=SENDER_B)],
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                )
            )
        )
        self.assertEqual(
            set(got["nextSenderNonceState"]), {SENDER_A}
        )

    def test_no_selection_keeps_state_but_normalizes_order(self):
        got = result_of(
            advance_batch_state(
                document(
                    [make_request(nonce=nonce_of(0, 1))],  # 无状态组 seq 1 不能成链
                    bundle_gas=ITEM_GAS,
                    bundle_cost=ITEM_COST,
                    nonce_state={
                        SENDER_B: [state_entry(0, 4)],
                        SENDER_A: [state_entry(9, 2), state_entry(1, 7)],
                    },
                )
            )
        )
        self.assertEqual(
            got["nextSenderNonceState"],
            {
                SENDER_A: [
                    {"nonceKey": "1", "lastSequence": "7"},
                    {"nonceKey": "9", "lastSequence": "2"},
                ],
                SENDER_B: [{"nonceKey": "0", "lastSequence": "4"}],
            },
        )


class TestNoMutation(unittest.TestCase):
    def test_input_not_mutated(self):
        doc = document(
            [
                make_request(nonce=nonce_of(0, 3)),
                make_request(nonce=nonce_of(0, 4)),
                make_request(sender=SENDER_B, nonce=nonce_of(0, 0)),
            ],
            bundle_gas=ITEM_GAS * 3,
            bundle_cost=ITEM_COST * 3,
            usage={SENDER_A: usage_entry(10, 20)},
            nonce_state={SENDER_A: [state_entry(0, 2)]},
        )
        snapshot = copy.deepcopy(doc)
        advance_batch_state(doc)
        self.assertEqual(doc, snapshot)


class TestErrorParity(unittest.TestCase):
    """任何不合法输入与规划入口返回完全相同的结果。"""

    def valid_doc(self):
        return document(
            [make_request()],
            bundle_gas=ITEM_GAS * 10,
            bundle_cost=ITEM_COST * 10,
        )

    def assert_same_error(self, doc):
        self.assertEqual(
            advance_batch_state(doc),
            plan_bundle_sender_budget_with_nonce_state(doc),
        )

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            with self.subTest(document=bad):
                self.assert_same_error(bad)

    def test_unknown_and_missing_fields(self):
        doc = self.valid_doc()
        doc["extra"] = 1
        self.assert_same_error(doc)
        self.assert_same_error({})
        self.assert_same_error({"requests": [], "extra": 1})

    def test_request_error(self):
        doc = self.valid_doc()
        del doc["requests"][0]["userOperation"]["signature"]
        self.assert_same_error(doc)

    def test_policy_error(self):
        doc = self.valid_doc()
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        self.assert_same_error(doc)

    def test_usage_error(self):
        doc = self.valid_doc()
        doc["senderUsage"] = {SENDER_A: {"totalGas": "01", "estimatedCostWei": "0"}}
        self.assert_same_error(doc)

    def test_nonce_state_error(self):
        doc = self.valid_doc()
        doc["senderNonceState"] = {
            SENDER_A: [{"nonceKey": "01", "lastSequence": "0"}]
        }
        self.assert_same_error(doc)

    def test_error_result_shape(self):
        err = error_of(advance_batch_state(self.valid_doc() | {"extra": 1}))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")
        self.assertEqual(set(err), {"code", "path", "message"})

    def test_check_order_usage_before_state(self):
        doc = self.valid_doc()
        doc["senderUsage"] = []
        doc["senderNonceState"] = []
        err = error_of(advance_batch_state(doc))
        self.assertEqual(err["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(err["path"], "/senderUsage")

    def test_exported_from_package(self):
        self.assertIs(paymaster.advance_batch_state, advance_batch_state)
        self.assertIn("advance_batch_state", paymaster.__all__)


class TestRandomCarryForward(unittest.TestCase):
    """随机实例：plan 与规划入口一致；结转数值独立按入选集合重算核对。"""

    def test_random_instances(self):
        rng = random.Random(20261007)
        senders = [SENDER_A, SENDER_B, SENDER_C]
        for _ in range(200):
            count = rng.randint(0, 6)
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
                bundle_gas=rng.randint(0, ITEM_GAS * 4),
                bundle_cost=rng.randint(0, ITEM_COST * 4),
                max_gas_per_sender=rng.randint(0, ITEM_GAS * 3),
                max_cost_per_sender=rng.randint(0, ITEM_COST * 3),
                usage=usage,
                nonce_state=nonce_state,
            )
            got = advance_batch_state(doc)
            planner = plan_bundle_sender_budget_with_nonce_state(doc)
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["plan"], planner["plan"])

            selected = set(got["plan"]["selected"])
            expect_usage: dict[str, dict[str, int]] = {}
            for key, entry in usage.items():
                expect_usage[key.lower()] = {
                    "totalGas": int(entry["totalGas"]),
                    "estimatedCostWei": int(entry["estimatedCostWei"]),
                }
            expect_state: dict[str, dict[int, int]] = {}
            for sender, entries in nonce_state.items():
                expect_state[sender] = {
                    int(entry["nonceKey"]): int(entry["lastSequence"])
                    for entry in entries
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

            want_usage = {
                sender: {
                    "totalGas": str(expect_usage[sender]["totalGas"]),
                    "estimatedCostWei": str(
                        expect_usage[sender]["estimatedCostWei"]
                    ),
                }
                for sender in sorted(expect_usage)
            }
            self.assertEqual(got["nextSenderUsage"], want_usage)

            want_state = {
                sender: [
                    {"nonceKey": str(key), "lastSequence": str(keys[key])}
                    for key in sorted(keys)
                ]
                for sender, keys in sorted(expect_state.items())
            }
            self.assertEqual(got["nextSenderNonceState"], want_state)


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.batch_state"],
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
        self.assertEqual(
            set(result),
            {"ok", "plan", "nextSenderUsage", "nextSenderNonceState"},
        )
        self.assertEqual(
            result["nextSenderNonceState"][SENDER_A],
            [{"nonceKey": "0", "lastSequence": "4"}],
        )

    def test_validation_error_exit_1(self):
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
