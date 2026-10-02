"""paymaster.validation 与 CLI 的单元测试。运行：python -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from paymaster.validation import (  # noqa: E402
    E_FIELD_COMBINATION,
    E_INVALID_FIELD,
    E_INVALID_JSON,
    E_MISSING_FIELD,
    E_UNKNOWN_FIELD,
    E_UNSUPPORTED_VERSION,
    validate,
)

SENDER = "0x1111111111111111111111111111111111111111"
ENTRY_POINT = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
FACTORY = "0x2222222222222222222222222222222222222222"
PAYMASTER = "0x3333333333333333333333333333333333333333"


def valid_request() -> dict:
    return {
        "userOperation": {
            "sender": SENDER,
            "nonce": "0x0",
            "callData": "0x",
            "callGasLimit": "0x10000",
            "verificationGasLimit": "0x20000",
            "preVerificationGas": "0x30000",
            "maxFeePerGas": "0x10",
            "maxPriorityFeePerGas": "0x10",
            "signature": "0x",
        },
        "context": {
            "version": "0.7",
            "chainId": "0x1",
            "entryPoint": ENTRY_POINT,
            "baseFeePerGas": "0x7",
        },
    }


def error_of(result: dict) -> dict:
    assert result["ok"] is False, f"expected failure, got {result}"
    return result["error"]


class TestValidRequests(unittest.TestCase):
    def test_minimal_request_ok(self):
        result = validate(valid_request())
        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result["normalized"]), {"userOperation", "context"}
        )

    def test_full_request_with_factory_and_paymaster_ok(self):
        req = valid_request()
        req["userOperation"].update(
            {
                "factory": FACTORY,
                "factoryData": "0xdeadBEEF",
                "paymaster": PAYMASTER,
                "paymasterVerificationGasLimit": "0x40000",
                "paymasterPostOpGasLimit": "0x50000",
                "paymasterData": "0x",
            }
        )
        result = validate(req)
        self.assertTrue(result["ok"], result)

    def test_normalization_lowercases_addresses_and_bytes(self):
        req = valid_request()
        req["userOperation"]["sender"] = "0x" + "aA" * 20
        req["userOperation"]["callData"] = "0xDeAdBeEf"
        req["userOperation"]["maxFeePerGas"] = "0xAB"
        req["context"]["entryPoint"] = ENTRY_POINT.upper().replace("0X", "0x")
        result = validate(req)
        self.assertTrue(result["ok"], result)
        uo = result["normalized"]["userOperation"]
        self.assertEqual(uo["sender"], "0x" + "aa" * 20)
        self.assertEqual(uo["callData"], "0xdeadbeef")
        self.assertEqual(uo["maxFeePerGas"], "0xab")
        self.assertEqual(
            result["normalized"]["context"]["entryPoint"], ENTRY_POINT.lower()
        )

    def test_normalized_omits_absent_optional_fields(self):
        result = validate(valid_request())
        uo = result["normalized"]["userOperation"]
        for key in (
            "factory",
            "factoryData",
            "paymaster",
            "paymasterVerificationGasLimit",
            "paymasterPostOpGasLimit",
            "paymasterData",
        ):
            self.assertNotIn(key, uo)

    def test_zero_quantity_allowed_for_nonce_and_fees(self):
        req = valid_request()
        req["userOperation"]["maxFeePerGas"] = "0x0"
        req["userOperation"]["maxPriorityFeePerGas"] = "0x0"
        req["context"]["baseFeePerGas"] = "0x0"
        self.assertTrue(validate(req)["ok"])

    def test_max_quantity_size_32_bytes_ok(self):
        req = valid_request()
        req["userOperation"]["nonce"] = "0x" + "ff" * 32
        self.assertTrue(validate(req)["ok"])


class TestUnknownAndMissingFields(unittest.TestCase):
    def test_unknown_root_field(self):
        req = valid_request()
        req["extra"] = 1
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/extra")

    def test_unknown_user_operation_field(self):
        req = valid_request()
        req["userOperation"]["initCode"] = "0x"
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/userOperation/initCode")

    def test_unknown_context_field(self):
        req = valid_request()
        req["context"]["gasPrice"] = "0x1"
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_UNKNOWN_FIELD)
        self.assertEqual(err["path"], "/context/gasPrice")

    def test_missing_root_field(self):
        req = valid_request()
        del req["context"]
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/context")

    def test_missing_required_user_operation_field(self):
        req = valid_request()
        del req["userOperation"]["signature"]
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/userOperation/signature")

    def test_missing_context_field(self):
        req = valid_request()
        del req["context"]["baseFeePerGas"]
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_MISSING_FIELD)
        self.assertEqual(err["path"], "/context/baseFeePerGas")


class TestInvalidValues(unittest.TestCase):
    def _invalid(self, mutate, expected_path):
        req = valid_request()
        mutate(req)
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_INVALID_FIELD)
        self.assertEqual(err["path"], expected_path)

    def test_root_not_object(self):
        for bad in ([], "x", 1, None, True):
            err = error_of(validate(bad))
            self.assertEqual(err["code"], E_INVALID_JSON)
            self.assertEqual(err["path"], "")

    def test_user_operation_not_object(self):
        self._invalid(
            lambda r: r.update(userOperation=[]), "/userOperation"
        )

    def test_address_wrong_length(self):
        self._invalid(
            lambda r: r["userOperation"].update(sender="0x" + "11" * 19),
            "/userOperation/sender",
        )

    def test_address_zero(self):
        self._invalid(
            lambda r: r["userOperation"].update(sender="0x" + "00" * 20),
            "/userOperation/sender",
        )

    def test_address_not_string(self):
        self._invalid(
            lambda r: r["userOperation"].update(sender=123),
            "/userOperation/sender",
        )

    def test_bytes_odd_length(self):
        self._invalid(
            lambda r: r["userOperation"].update(callData="0xabc"),
            "/userOperation/callData",
        )

    def test_bytes_missing_prefix(self):
        self._invalid(
            lambda r: r["userOperation"].update(callData="deadbeef"),
            "/userOperation/callData",
        )

    def test_quantity_leading_zero(self):
        self._invalid(
            lambda r: r["userOperation"].update(nonce="0x01"),
            "/userOperation/nonce",
        )

    def test_quantity_bare_prefix(self):
        self._invalid(
            lambda r: r["userOperation"].update(nonce="0x"),
            "/userOperation/nonce",
        )

    def test_quantity_exceeds_32_bytes(self):
        self._invalid(
            lambda r: r["userOperation"].update(nonce="0x" + "1" + "0" * 64),
            "/userOperation/nonce",
        )

    def test_quantity_not_string(self):
        self._invalid(
            lambda r: r["userOperation"].update(nonce=7),
            "/userOperation/nonce",
        )

    def test_gas_limit_zero(self):
        for field in ("callGasLimit", "verificationGasLimit", "preVerificationGas"):
            with self.subTest(field=field):
                self._invalid(
                    lambda r, f=field: r["userOperation"].update({f: "0x0"}),
                    f"/userOperation/{field}",
                )

    def test_chain_id_zero(self):
        self._invalid(
            lambda r: r["context"].update(chainId="0x0"), "/context/chainId"
        )

    def test_paymaster_gas_limit_zero(self):
        def mutate(r):
            r["userOperation"].update(
                paymaster=PAYMASTER,
                paymasterVerificationGasLimit="0x0",
                paymasterPostOpGasLimit="0x50000",
                paymasterData="0x",
            )

        self._invalid(mutate, "/userOperation/paymasterVerificationGasLimit")

    def test_priority_fee_exceeds_max_fee(self):
        def mutate(r):
            r["userOperation"].update(
                maxFeePerGas="0x10", maxPriorityFeePerGas="0x11"
            )

        self._invalid(mutate, "/userOperation/maxPriorityFeePerGas")

    def test_version_not_string(self):
        self._invalid(
            lambda r: r["context"].update(version=0.7), "/context/version"
        )


class TestVersion(unittest.TestCase):
    def test_unsupported_version(self):
        req = valid_request()
        req["context"]["version"] = "0.6"
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_UNSUPPORTED_VERSION)
        self.assertEqual(err["path"], "/context/version")


class TestFieldCombinations(unittest.TestCase):
    def _combination(self, mutate, expected_path):
        req = valid_request()
        mutate(req)
        err = error_of(validate(req))
        self.assertEqual(err["code"], E_FIELD_COMBINATION)
        self.assertEqual(err["path"], expected_path)

    def test_factory_without_factory_data(self):
        self._combination(
            lambda r: r["userOperation"].update(factory=FACTORY),
            "/userOperation/factoryData",
        )

    def test_factory_data_without_factory(self):
        self._combination(
            lambda r: r["userOperation"].update(factoryData="0x"),
            "/userOperation/factory",
        )

    def test_paymaster_partial_group(self):
        self._combination(
            lambda r: r["userOperation"].update(
                paymaster=PAYMASTER, paymasterData="0x"
            ),
            "/userOperation/paymasterVerificationGasLimit",
        )

    def test_paymaster_fields_without_paymaster_address(self):
        self._combination(
            lambda r: r["userOperation"].update(
                paymasterVerificationGasLimit="0x40000",
                paymasterPostOpGasLimit="0x50000",
                paymasterData="0x",
            ),
            "/userOperation/paymaster",
        )

    def test_paymaster_data_alone_rejected(self):
        self._combination(
            lambda r: r["userOperation"].update(paymasterData="0x"),
            "/userOperation/paymaster",
        )


class TestCli(unittest.TestCase):
    def run_cli(self, stdin_text: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "paymaster.validate"],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, proc.stdout

    def test_valid_request_exit_0_single_json_doc(self):
        code, out = self.run_cli(json.dumps(valid_request()))
        self.assertEqual(code, 0)
        docs = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(docs), 1)
        result = json.loads(out)
        self.assertTrue(result["ok"])

    def test_invalid_request_exit_1(self):
        req = valid_request()
        del req["userOperation"]["sender"]
        code, out = self.run_cli(json.dumps(req))
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["error"]["code"], E_MISSING_FIELD)

    def test_malformed_json_exit_1(self):
        code, out = self.run_cli("{not json")
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["error"]["code"], E_INVALID_JSON)
        self.assertEqual(result["error"]["path"], "")

    def test_non_object_json_exit_1(self):
        code, out = self.run_cli("[1, 2, 3]")
        self.assertEqual(code, 1)
        result = json.loads(out)
        self.assertEqual(result["error"]["code"], E_INVALID_JSON)


if __name__ == "__main__":
    unittest.main()
