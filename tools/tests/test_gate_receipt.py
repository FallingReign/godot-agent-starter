"""Nonce-bound gate receipt tests; no engine or provider is launched."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

import check


ROOT = Path(__file__).resolve().parents[2]


class GateReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = ROOT / ".checklogs" / f"gate-receipt-{uuid.uuid4().hex}"
        self.scratch.mkdir(parents=True)

    def tearDown(self) -> None:
        resolved = self.scratch.resolve()
        expected = (ROOT / ".checklogs").resolve()
        if resolved.parent != expected or not resolved.name.startswith("gate-receipt-"):
            raise AssertionError(f"refusing to remove unexpected path: {resolved}")
        shutil.rmtree(resolved)

    def test_finish_writes_the_exact_nonce_bound_receipt(self) -> None:
        nonce = "a" * 32
        auth_key = "b" * 64
        repository_sha256 = "c" * 64
        results = check.Results()
        results.passed("synthetic")
        with mock.patch.object(check, "LOG_DIR", self.scratch), mock.patch.object(
            check, "VERIFY_RUN_ID", nonce
        ), mock.patch.object(check, "VERIFY_RUN_ID_VALID", True), mock.patch.object(
            check, "VERIFY_AUTH_KEY", auth_key
        ), mock.patch.object(check, "VERIFY_AUTH_KEY_VALID", True), mock.patch.object(
            check, "VERIFY_REPOSITORY_SHA256", repository_sha256
        ), mock.patch.object(
            check, "VERIFY_REPOSITORY_SHA256_VALID", True
        ), mock.patch.object(
            check, "RESULTS", results
        ), mock.patch.object(check, "retro_nudge"):
            with contextlib.redirect_stdout(io.StringIO()):
                code = check.finish()

        self.assertEqual(0, code)
        receipt = json.loads(
            (self.scratch / f"run-summary-{nonce}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(nonce, receipt["run_id"])
        self.assertEqual(2, receipt["schema"])
        self.assertEqual(repository_sha256, receipt["repository_sha256"])
        self.assertFalse(receipt["failed"])
        supplied = receipt.pop("auth_sha256")
        canonical = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(
            hmac.new(bytes.fromhex(auth_key), canonical, hashlib.sha256).hexdigest(),
            supplied,
        )

    def test_finish_fails_when_the_receipt_cannot_be_persisted(self) -> None:
        results = check.Results()
        results.passed("synthetic")
        with mock.patch.object(check, "LOG_DIR", self.scratch), mock.patch.object(
            check, "VERIFY_RUN_ID", "b" * 32
        ), mock.patch.object(check, "VERIFY_RUN_ID_VALID", True), mock.patch.object(
            check, "VERIFY_AUTH_KEY", "c" * 64
        ), mock.patch.object(check, "VERIFY_AUTH_KEY_VALID", True), mock.patch.object(
            check, "VERIFY_REPOSITORY_SHA256", "d" * 64
        ), mock.patch.object(
            check, "VERIFY_REPOSITORY_SHA256_VALID", True
        ), mock.patch.object(
            check, "RESULTS", results
        ), mock.patch.object(Path, "write_text", side_effect=OSError("locked")):
            with contextlib.redirect_stdout(io.StringIO()):
                code = check.finish()

        self.assertEqual(1, code)

    def test_malformed_public_nonce_fails_closed(self) -> None:
        results = check.Results()
        results.passed("synthetic")
        with mock.patch.object(check, "LOG_DIR", self.scratch), mock.patch.object(
            check, "VERIFY_RUN_ID", "../shared"
        ), mock.patch.object(check, "VERIFY_RUN_ID_VALID", False), mock.patch.object(
            check, "RESULTS", results
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                code = check.finish()

        self.assertEqual(1, code)
        self.assertEqual([], list(self.scratch.iterdir()))

    def test_public_receipt_without_authentication_material_fails_closed(self) -> None:
        results = check.Results()
        results.passed("synthetic")
        with mock.patch.object(check, "LOG_DIR", self.scratch), mock.patch.object(
            check, "VERIFY_RUN_ID", "d" * 32
        ), mock.patch.object(check, "VERIFY_RUN_ID_VALID", True), mock.patch.object(
            check, "VERIFY_AUTH_KEY", ""
        ), mock.patch.object(check, "VERIFY_AUTH_KEY_VALID", False), mock.patch.object(
            check, "VERIFY_REPOSITORY_SHA256", ""
        ), mock.patch.object(
            check, "VERIFY_REPOSITORY_SHA256_VALID", False
        ), mock.patch.object(check, "RESULTS", results):
            with contextlib.redirect_stdout(io.StringIO()):
                code = check.finish()

        self.assertEqual(1, code)
        self.assertEqual([], list(self.scratch.iterdir()))


if __name__ == "__main__":
    unittest.main()
