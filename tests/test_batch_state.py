"""paymaster.batch_state.advance_batch_state 与其 CLI 的单元测试。

运行：python -m unittest tests.test_batch_state -v
"""

from __future__ import annotations

import copy
import json
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
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
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
    bundle_gas: int = 0x1000000,
    bundle_cost: int = 0xDE0B6B3A7640000,
    max_gas_per_sender: int = 0x1000000,
    max_cost_per_sender: int = 0xDE0B6B3A7640000,
    usage: dict | None = None,
    nonce_state: dict | None = None,
) -> dict:
    return {
        "requests": requests,
        "sponsorshipPolicy": {
            "budgetWei": hex(0xDE0B6B3A7640000),
            "maxTotalGas": hex(0x1000000),
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


class TestAdvanceBatchState(unittest.TestCase):
    def test_top_level_keys_and_plan_passthrough(self):
        doc = document(
            [make_request(nonce=0), make_request(nonce=1)],
        )
        result = advance_batch_state(doc)
        self.assertEqual(
            list(result),
            ["ok", "plan", "nextSenderUsage", "nextSenderNonceState"],
        )
        plan = plan_bundle_sender_budget_with_nonce_state(doc)
        self.assertEqual(result["plan"], plan["plan"])
        self.assertEqual(result["plan"]["selected"], [0, 1])
        self.assertEqual(result["plan"]["operationCount"], "2")
        self.assertEqual(result["plan"]["totalGas"], str(2 * ITEM_GAS))
        self.assertEqual(result["plan"]["estimatedCostWei"], str(2 * ITEM_COST))

    def test_usage_accumulates_and_preserves(self):
        doc = document(
            [make_request(sender=SENDER_B, nonce=0), make_request(nonce=0)],
            usage={
                SENDER_A: usage_entry(10, 20),
                SENDER_C: usage_entry(7, 9),
            },
        )
        result = advance_batch_state(doc)
        self.assertEqual(
            result["nextSenderUsage"],
            {
                SENDER_A: usage_entry(10 + ITEM_GAS, 20 + ITEM_COST),
                SENDER_B: usage_entry(ITEM_GAS, ITEM_COST),
                SENDER_C: usage_entry(7, 9),
            },
        )
        # sender 键按规范小写地址升序。
        self.assertEqual(
            list(result["nextSenderUsage"]), [SENDER_A, SENDER_B, SENDER_C]
        )

    def test_usage_key_normalized_to_lowercase(self):
        mixed = "0xaA11111111111111111111111111111111111111"
        doc = document([], usage={mixed: usage_entry(3, 4)})
        result = advance_batch_state(doc)
        self.assertEqual(
            result["nextSenderUsage"],
            {"0xaa11111111111111111111111111111111111111": usage_entry(3, 4)},
        )

    def test_nonce_state_advances_and_preserves(self):
        doc = document(
            [
                make_request(nonce=nonce_of(0, 5)),
                make_request(nonce=nonce_of(0, 6)),
                make_request(nonce=nonce_of(2, 0)),
            ],
            nonce_state={
                SENDER_A: [state_entry(0, 4), state_entry(1, 9)],
                SENDER_B: [state_entry(3, 3)],
            },
        )
        result = advance_batch_state(doc)
        self.assertEqual(result["plan"]["selected"], [0, 1, 2])
        self.assertEqual(
            result["nextSenderNonceState"],
            {
                SENDER_A: [state_entry(0, 6), state_entry(1, 9), state_entry(2, 0)],
                SENDER_B: [state_entry(3, 3)],
            },
        )

    def test_nonce_state_entries_sorted_by_numeric_key(self):
        doc = document(
            [make_request(nonce=nonce_of(10, 0)), make_request(nonce=nonce_of(2, 0))],
            nonce_state={SENDER_A: [state_entry(9, 0)]},
        )
        result = advance_batch_state(doc)
        entries = result["nextSenderNonceState"][SENDER_A]
        self.assertEqual(
            entries,
            [state_entry(2, 0), state_entry(9, 0), state_entry(10, 0)],
        )

    def test_new_sender_added_to_nonce_state(self):
        doc = document([make_request(sender=SENDER_B, nonce=nonce_of(1, 0))])
        result = advance_batch_state(doc)
        self.assertEqual(
            result["nextSenderNonceState"],
            {SENDER_B: [state_entry(1, 0)]},
        )
        self.assertEqual(
            result["nextSenderUsage"],
            {SENDER_B: usage_entry(ITEM_GAS, ITEM_COST)},
        )

    def test_skipped_items_do_not_advance_state(self):
        # 第二项 sequence 跳号断链，不入选，状态只结转到 0。
        doc = document(
            [make_request(nonce=0), make_request(nonce=2)],
        )
        result = advance_batch_state(doc)
        self.assertEqual(result["plan"]["selected"], [0])
        self.assertEqual(
            result["plan"]["skipped"], [{"index": 1, "reason": "E_NONCE_GAP"}]
        )
        self.assertEqual(
            result["nextSenderNonceState"], {SENDER_A: [state_entry(0, 0)]}
        )
        self.assertEqual(
            result["nextSenderUsage"],
            {SENDER_A: usage_entry(ITEM_GAS, ITEM_COST)},
        )

    def test_unapproved_items_do_not_advance_state(self):
        # 单笔 gas 超 sponsorshipPolicy.maxTotalGas，approved 为 false。
        doc = document([make_request(call_gas=0x2000000, nonce=0)])
        result = advance_batch_state(doc)
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["plan"]["skipped"], [{"index": 0, "reason": "E_GAS_LIMIT"}]
        )
        self.assertEqual(result["nextSenderUsage"], {})
        self.assertEqual(result["nextSenderNonceState"], {})

    def test_empty_requests_success_and_states_preserved(self):
        doc = document(
            [],
            usage={SENDER_A: usage_entry(5, 6)},
            nonce_state={SENDER_A: [state_entry(0, 4)], SENDER_B: []},
        )
        result = advance_batch_state(doc)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["nextSenderUsage"], {SENDER_A: usage_entry(5, 6)}
        )
        self.assertEqual(
            result["nextSenderNonceState"],
            {SENDER_A: [state_entry(0, 4)], SENDER_B: []},
        )

    def test_decimal_strings_no_leading_zeros(self):
        doc = document([make_request(nonce=0)])
        result = advance_batch_state(doc)
        entry = result["nextSenderUsage"][SENDER_A]
        self.assertEqual(entry["totalGas"], str(ITEM_GAS))
        self.assertEqual(entry["estimatedCostWei"], str(ITEM_COST))
        state = result["nextSenderNonceState"][SENDER_A][0]
        self.assertEqual(state, {"nonceKey": "0", "lastSequence": "0"})

    def test_input_not_mutated(self):
        doc = document(
            [make_request(nonce=0), make_request(nonce=1)],
            usage={SENDER_A: usage_entry(1, 2)},
            nonce_state={SENDER_A: [state_entry(0, 0)]},
        )
        snapshot = copy.deepcopy(doc)
        advance_batch_state(doc)
        self.assertEqual(doc, snapshot)

    def test_error_results_match_planning_entry(self):
        cases = []
        bad_request = make_request()
        del bad_request["userOperation"]["nonce"]
        cases.append(document([bad_request]))
        doc = document([make_request()])
        doc["senderUsage"] = {SENDER_A: {"totalGas": "01", "estimatedCostWei": "0"}}
        cases.append(doc)
        doc = document([make_request()])
        doc["senderNonceState"] = {SENDER_A: [state_entry(0, 0), state_entry(0, 1)]}
        cases.append(doc)
        doc = document([make_request()])
        del doc["senderNonceState"]
        cases.append(doc)
        doc = document([make_request()])
        doc["bundlePolicy"]["maxTotalGas"] = "0x0"
        cases.append(doc)
        for doc in cases:
            self.assertEqual(
                advance_batch_state(doc),
                plan_bundle_sender_budget_with_nonce_state(doc),
            )

    def test_error_codes(self):
        doc = document([make_request()])
        doc["senderUsage"] = []
        result = advance_batch_state(doc)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], E_USAGE_INVALID_FIELD)
        self.assertEqual(result["error"]["path"], "/senderUsage")

        doc = document([make_request()])
        doc["senderNonceState"] = {SENDER_A: [{"nonceKey": "0"}]}
        result = advance_batch_state(doc)
        self.assertEqual(result["error"]["code"], E_NONCE_STATE_INVALID_FIELD)
        self.assertEqual(
            result["error"]["path"],
            "/senderNonceState/" + SENDER_A + "/0/lastSequence",
        )

        result = advance_batch_state({"requests": []})
        self.assertEqual(result["error"]["code"], E_MISSING_FIELD)
        self.assertEqual(result["error"]["path"], "/sponsorshipPolicy")

        result = advance_batch_state([1, 2])
        self.assertEqual(result["error"]["code"], E_INVALID_JSON)
        self.assertEqual(result["error"]["path"], "")

        doc = document([make_request()])
        doc["requests"] = {}
        result = advance_batch_state(doc)
        self.assertEqual(result["error"]["code"], E_INVALID_FIELD)
        self.assertEqual(result["error"]["path"], "/requests")

    def test_exported_from_package(self):
        import paymaster.batch_state

        self.assertIs(
            paymaster.batch_state.advance_batch_state, advance_batch_state
        )


class TestBatchStateCli(unittest.TestCase):
    def run_cli(self, stdin_text: str):
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.batch_state"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr

    def test_success_exit_0(self):
        doc = document(
            [make_request(nonce=0)],
            usage={SENDER_A: usage_entry(1, 1)},
            nonce_state={SENDER_A: [state_entry(0, 0)]},
        )
        # nonce 0 不高于 lastSequence 0，不入选；状态不变。
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan"]["selected"], [])
        self.assertEqual(
            result["plan"]["skipped"], [{"index": 0, "reason": "E_NONCE_CONFLICT"}]
        )
        self.assertEqual(
            result["nextSenderUsage"], {SENDER_A: usage_entry(1, 1)}
        )
        self.assertEqual(
            result["nextSenderNonceState"], {SENDER_A: [state_entry(0, 0)]}
        )

    def test_success_advances_exit_0(self):
        doc = document([make_request(nonce=nonce_of(0, 0))])
        code, out, _ = self.run_cli(json.dumps(doc))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(
            list(result),
            ["ok", "plan", "nextSenderUsage", "nextSenderNonceState"],
        )
        self.assertEqual(
            result["nextSenderNonceState"], {SENDER_A: [state_entry(0, 0)]}
        )

    def test_invalid_document_exit_1(self):
        doc = document([make_request()])
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
