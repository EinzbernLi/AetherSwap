from __future__ import annotations

import json
import ast
import re
import socket
import ssl
import unittest
from pathlib import Path

from tests import sent_history_probe_outcome as outcome

try:
    import requests
except Exception:  # pragma: no cover - environment-dependent
    requests = None

try:
    import urllib3
except Exception:  # pragma: no cover - environment-dependent
    urllib3 = None


class FakeResponse:
    def __init__(self, status_code, payload=None, error=None):
        self.status_code = status_code
        self._payload = payload
        self._error = error

    def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


class PropertyResponse:
    def __init__(self, error):
        self._error = error

    @property
    def status_code(self):
        raise self._error


class ControlJSONResponse:
    status_code = 200

    def __init__(self, error):
        self._error = error

    def json(self):
        raise self._error


class MarkerError(Exception):
    def __str__(self):
        return "SECRET_MARKER"


class VerifyCodeReadError(ssl.SSLCertVerificationError):
    @property
    def verify_code(self):
        raise RuntimeError("SENTINEL_TEXT")


class ProbeOutcomeTests(unittest.TestCase):
    def run_once(self, response, validator=None):
        calls = []

        def send_once():
            calls.append(True)
            if isinstance(response, BaseException):
                raise response
            return response

        result = outcome.probe_once(send_once, validator)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.request_count, 1)
        if result.failure_class is not outcome.FailureClass.SUCCESS:
            self.assertIsNone(result._payload)
            self.assertFalse(hasattr(result, "response"))
            self.assertFalse(hasattr(result, "exception"))
        return result

    def assert_public_shape(self, result):
        self.assertEqual(
            set(result.public_dict()),
            {
                "failure_class",
                "request_reached_server",
                "http_status_class",
                "request_count",
                "json_parsed",
                "schema_valid",
                "tls_subtype",
                "tls_verify_code",
            },
        )
        self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_non_tls_outcome_uses_none_tls_subtype(self):
        result = self.run_once(FakeResponse(200, {"safe": True}))
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.NONE)
        self.assertEqual(result.public_dict()["tls_subtype"], "NONE")

        result = self.run_once(socket.gaierror("SECRET_MARKER"))
        self.assertEqual(result.failure_class, outcome.FailureClass.DNS)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.NONE)

    def test_tls_subtypes_use_exception_types_only(self):
        cases = (
            (ssl.SSLCertVerificationError("SECRET_MARKER"), outcome.TLSSubtype.CERT_VERIFY),
            (ssl.SSLEOFError("SECRET_MARKER"), outcome.TLSSubtype.EOF),
            (ssl.SSLZeroReturnError("SECRET_MARKER"), outcome.TLSSubtype.ZERO_RETURN),
            (ssl.SSLSyscallError("SECRET_MARKER"), outcome.TLSSubtype.SYSCALL),
            (ssl.SSLWantReadError("SECRET_MARKER"), outcome.TLSSubtype.UNKNOWN),
            (ssl.SSLError("SECRET_MARKER"), outcome.TLSSubtype.UNKNOWN),
        )
        for exception, subtype in cases:
            with self.subTest(exception=type(exception).__name__):
                result = self.run_once(exception)
                self.assertEqual(result.failure_class, outcome.FailureClass.TLS)
                self.assertEqual(result.tls_subtype, subtype)
                self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_tls_verify_code_is_bounded_and_text_free(self):
        direct = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        direct.verify_code = 42
        result = self.run_once(direct)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.CERT_VERIFY)
        self.assertEqual(result.tls_verify_code, 42)
        self.assertEqual(result.public_dict()["tls_verify_code"], 42)
        self.assertNotIn("SENTINEL_TEXT", result.public_json())

        wrapped_cert = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        wrapped_cert.verify_code = 43
        wrapped = requests.exceptions.SSLError("SENTINEL_TEXT", wrapped_cert) if requests else ssl.SSLError("SENTINEL_TEXT", wrapped_cert)
        result = self.run_once(wrapped)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.CERT_VERIFY)
        self.assertEqual(result.tls_verify_code, 43)
        self.assertNotIn("SENTINEL_TEXT", result.public_json())

        for value in (None, True, False, 0, 256, "44"):
            invalid = ssl.SSLCertVerificationError("SENTINEL_TEXT")
            invalid.verify_code = value
            result = self.run_once(invalid)
            self.assertIsNone(result.tls_verify_code)

        first = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        first.verify_code = 45
        second = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        second.verify_code = 46
        conflict = ssl.SSLError("SENTINEL_TEXT")
        conflict.args = ("SENTINEL_TEXT", first, second)
        result = self.run_once(conflict)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.CERT_VERIFY)
        self.assertIsNone(result.tls_verify_code)

        valid = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        valid.verify_code = 50
        mixed = ssl.SSLError("SENTINEL_TEXT")
        mixed.args = ("SENTINEL_TEXT", valid, ssl.SSLCertVerificationError("SENTINEL_TEXT"))
        result = self.run_once(mixed)
        self.assertIsNone(result.tls_verify_code)

        readable = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        readable.verify_code = 51
        unreadable = VerifyCodeReadError("SENTINEL_TEXT")
        mixed = ssl.SSLError("SENTINEL_TEXT")
        mixed.args = ("SENTINEL_TEXT", readable, unreadable)
        result = self.run_once(mixed)
        self.assertIsNone(result.tls_verify_code)
        self.assertNotIn("SENTINEL_TEXT", result.public_json())

        try:
            raise ssl.SSLCertVerificationError("SENTINEL_TEXT")
        except ssl.SSLCertVerificationError as inner:
            inner.verify_code = 47
            try:
                raise ssl.SSLError("SENTINEL_TEXT") from None
            except ssl.SSLError as suppressed:
                result = self.run_once(suppressed)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.UNKNOWN)
        self.assertIsNone(result.tls_verify_code)

        current = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        current.verify_code = 48
        for _ in range(7):
            try:
                raise RuntimeError("SENTINEL_TEXT") from current
            except RuntimeError as wrapped_error:
                current = wrapped_error
        result = self.run_once(current)
        self.assertEqual(result.tls_verify_code, 48)

        current = ssl.SSLCertVerificationError("SENTINEL_TEXT")
        current.verify_code = 49
        for _ in range(8):
            try:
                raise RuntimeError("SENTINEL_TEXT") from current
            except RuntimeError as wrapped_error:
                current = wrapped_error
        result = self.run_once(current)
        self.assertIsNone(result.tls_verify_code)

    @unittest.skipUnless(requests is not None, "requests is unavailable")
    def test_requests_wrapper_args_reach_certificate_subtype(self):
        wrapped = requests.exceptions.SSLError(
            "SECRET_MARKER", ssl.SSLCertVerificationError("SECRET_MARKER")
        )
        result = self.run_once(wrapped)
        self.assertEqual(result.failure_class, outcome.FailureClass.TLS)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.CERT_VERIFY)
        self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_legacy_sixth_positional_argument_is_payload(self):
        payload = {"SECRET_MARKER": True}
        result = outcome.ProbeOutcome(
            outcome.FailureClass.SUCCESS,
            outcome.ReachedServer.YES,
            outcome.HttpStatusClass.S2XX,
            True,
            None,
            payload,
        )
        self.assertIs(result.get_payload(), payload)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.NONE)
        self.assertIsNone(result.tls_verify_code)
        self.assertNotIn("payload", result.public_dict())
        self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_exception_args_do_not_use_non_exception_text(self):
        wrapped = ssl.SSLError("CERTIFICATE_VERIFY_FAILED SECRET_MARKER")
        result = self.run_once(wrapped)
        self.assertEqual(result.failure_class, outcome.FailureClass.TLS)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.UNKNOWN)
        self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_from_none_suppresses_context_subtype(self):
        try:
            raise ssl.SSLCertVerificationError("SECRET_MARKER")
        except ssl.SSLCertVerificationError:
            try:
                raise ssl.SSLError("SECRET_MARKER") from None
            except ssl.SSLError as wrapped:
                result = self.run_once(wrapped)
        self.assertEqual(result.failure_class, outcome.FailureClass.TLS)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.UNKNOWN)
        self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_exception_subtype_depth_is_bounded(self):
        current = ssl.SSLCertVerificationError("SECRET_MARKER")
        for _ in range(7):
            try:
                raise RuntimeError("SECRET_MARKER") from current
            except RuntimeError as wrapped:
                current = wrapped
        result = self.run_once(current)
        self.assertEqual(result.failure_class, outcome.FailureClass.TLS)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.CERT_VERIFY)

        current = ssl.SSLCertVerificationError("SECRET_MARKER")
        for _ in range(8):
            try:
                raise RuntimeError("SECRET_MARKER") from current
            except RuntimeError as wrapped:
                current = wrapped
        result = self.run_once(current)
        self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.NONE)
        self.assertNotIn("SECRET_MARKER", result.public_json())

    @unittest.skipUnless(urllib3 is not None, "urllib3 is unavailable")
    def test_urllib3_wrapper_args_reach_certificate_subtype(self):
        wrapped = urllib3.exceptions.SSLError(
            "SECRET_MARKER", ssl.SSLCertVerificationError("SECRET_MARKER")
        )
        result = self.run_once(wrapped)
        self.assertEqual(result.failure_class, outcome.FailureClass.TLS)
        self.assertEqual(result.tls_subtype, outcome.TLSSubtype.CERT_VERIFY)
        self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_http_categories(self):
        cases = [
            (300, outcome.FailureClass.REDIRECT, outcome.HttpStatusClass.S3XX),
            (401, outcome.FailureClass.HTTP_AUTH, outcome.HttpStatusClass.S4XX),
            (403, outcome.FailureClass.HTTP_AUTH, outcome.HttpStatusClass.S4XX),
            (404, outcome.FailureClass.HTTP_OTHER, outcome.HttpStatusClass.S4XX),
            (500, outcome.FailureClass.HTTP_OTHER, outcome.HttpStatusClass.S5XX),
            (600, outcome.FailureClass.HTTP_OTHER, outcome.HttpStatusClass.OTHER),
        ]
        for status, failure, status_class in cases:
            with self.subTest(status=status):
                result = self.run_once(FakeResponse(status, {"SECRET_MARKER": True}))
                self.assertEqual(result.failure_class, failure)
                self.assertEqual(result.http_status_class, status_class)
                self.assertEqual(result.request_reached_server, outcome.ReachedServer.YES)
                self.assertFalse(result.json_parsed)
                self.assert_public_shape(result)
                self.assertNotIn(str(status), result.public_json())

    def test_json_and_schema_failures(self):
        result = self.run_once(FakeResponse(200, error=MarkerError("SECRET_MARKER")))
        self.assertEqual(result.failure_class, outcome.FailureClass.JSON)
        self.assertEqual(result.request_reached_server, outcome.ReachedServer.YES)
        self.assertTrue(result.http_status_class is outcome.HttpStatusClass.S2XX)
        self.assertFalse(result.json_parsed)
        self.assertIsNone(result.schema_valid)
        self.assert_public_shape(result)
        self.assertNotIn("SECRET_MARKER", repr(result))
        with self.assertRaises(LookupError):
            result.get_payload()

        result = self.run_once(FakeResponse(200, {"SECRET_MARKER": True}), lambda _: False)
        self.assertEqual(result.failure_class, outcome.FailureClass.SCHEMA)
        self.assertTrue(result.json_parsed)
        self.assertFalse(result.schema_valid)
        self.assertNotIn("SECRET_MARKER", repr(result))

        def raises_schema(_):
            raise outcome.SchemaValidationError("SECRET_MARKER")

        result = self.run_once(FakeResponse(200, {"SECRET_MARKER": True}), raises_schema)
        self.assertEqual(result.failure_class, outcome.FailureClass.SCHEMA)
        self.assertFalse(result.schema_valid)

        result = self.run_once(
            FakeResponse(200, {"SECRET_MARKER": True}),
            lambda _: (_ for _ in ()).throw(MarkerError("SECRET_MARKER")),
        )
        self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)
        self.assertIsNone(result.schema_valid)
        self.assertNotIn("SECRET_MARKER", repr(result))

    def test_response_access_failure_is_not_network_failure(self):
        for error in (
            PermissionError("SECRET_MARKER"),
            socket.gaierror("SECRET_MARKER"),
            TimeoutError("SECRET_MARKER"),
        ):
            with self.subTest(error=type(error).__name__):
                result = self.run_once(PropertyResponse(error))
                self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)
                self.assertEqual(result.request_reached_server, outcome.ReachedServer.UNKNOWN)
                self.assertEqual(result.http_status_class, outcome.HttpStatusClass.NONE)
                self.assertFalse(result.json_parsed)
                self.assertIsNone(result.schema_valid)

        for status_code in (None, "200", 200.0, True, object()):
            with self.subTest(status_code=type(status_code).__name__):
                result = self.run_once(FakeResponse(status_code, {"SECRET_MARKER": True}))
                self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)
                self.assertEqual(result.request_reached_server, outcome.ReachedServer.UNKNOWN)
                self.assertEqual(result.http_status_class, outcome.HttpStatusClass.NONE)

    def test_success_payload_is_only_explicitly_available(self):
        payload = {"SECRET_MARKER": {"item": 1}}
        result = self.run_once(FakeResponse(200, payload))
        self.assertEqual(result.failure_class, outcome.FailureClass.SUCCESS)
        self.assertEqual(result.request_reached_server, outcome.ReachedServer.YES)
        self.assertTrue(result.json_parsed)
        self.assertIsNone(result.schema_valid)
        self.assertEqual(result.get_payload(), payload)
        self.assertNotIn("SECRET_MARKER", repr(result))
        self.assertNotIn("SECRET_MARKER", result.public_json())

        result = self.run_once(FakeResponse(200, payload), lambda _: True)
        self.assertEqual(result.failure_class, outcome.FailureClass.SUCCESS)
        self.assertTrue(result.schema_valid)

    def test_schema_validator_requires_exact_bool(self):
        for invalid in ("true", 1, [], None):
            with self.subTest(value=type(invalid).__name__):
                result = self.run_once(FakeResponse(200, {"safe": True}), lambda _, value=invalid: value)
                self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)
                self.assertEqual(result.request_reached_server, outcome.ReachedServer.YES)
                self.assertTrue(result.json_parsed)
                self.assertIsNone(result.schema_valid)

    def test_network_exception_categories(self):
        cases = [
            (socket.gaierror("SECRET_MARKER"), outcome.FailureClass.DNS, outcome.ReachedServer.NO),
            (ssl.SSLError("SECRET_MARKER"), outcome.FailureClass.TLS, outcome.ReachedServer.UNKNOWN),
            (TimeoutError("SECRET_MARKER"), outcome.FailureClass.TIMEOUT, outcome.ReachedServer.UNKNOWN),
            (PermissionError("SECRET_MARKER"), outcome.FailureClass.LOCAL_PERMISSION, outcome.ReachedServer.NO),
            (ValueError("SECRET_MARKER"), outcome.FailureClass.UNEXPECTED, outcome.ReachedServer.UNKNOWN),
        ]
        if requests is not None:
            cases.extend(
                [
                    (requests.exceptions.ConnectTimeout("SECRET_MARKER"), outcome.FailureClass.CONNECT, outcome.ReachedServer.UNKNOWN),
                    (requests.exceptions.ReadTimeout("SECRET_MARKER"), outcome.FailureClass.TIMEOUT, outcome.ReachedServer.UNKNOWN),
                    (requests.exceptions.SSLError("SECRET_MARKER"), outcome.FailureClass.TLS, outcome.ReachedServer.UNKNOWN),
                    (requests.exceptions.ConnectionError("SECRET_MARKER"), outcome.FailureClass.CONNECT, outcome.ReachedServer.UNKNOWN),
                    (requests.exceptions.RequestException("SECRET_MARKER"), outcome.FailureClass.UNEXPECTED, outcome.ReachedServer.UNKNOWN),
                ]
            )
        for exception, failure, reached in cases:
            with self.subTest(exception=type(exception).__name__):
                result = self.run_once(exception)
                self.assertEqual(result.failure_class, failure)
                self.assertEqual(result.request_reached_server, reached)
                self.assertEqual(result.http_status_class, outcome.HttpStatusClass.NONE)
                self.assertFalse(result.json_parsed)
                self.assertIsNone(result.schema_valid)
                self.assertNotIn("SECRET_MARKER", repr(result))
                self.assertNotIn("SECRET_MARKER", result.public_json())

    def test_control_exceptions_propagate_at_each_stage(self):
        for control in (KeyboardInterrupt("SECRET_MARKER"), SystemExit("SECRET_MARKER"), GeneratorExit()):
            with self.subTest(stage="send_once", control=type(control).__name__):
                with self.assertRaises(type(control)):
                    outcome.probe_once(lambda error=control: (_ for _ in ()).throw(error))

            with self.subTest(stage="status_code", control=type(control).__name__):
                with self.assertRaises(type(control)):
                    outcome.probe_once(lambda error=control: PropertyResponse(error))

            with self.subTest(stage="json", control=type(control).__name__):
                with self.assertRaises(type(control)):
                    outcome.probe_once(lambda error=control: ControlJSONResponse(error))

            with self.subTest(stage="schema", control=type(control).__name__):
                with self.assertRaises(type(control)):
                    outcome.probe_once(
                        lambda: FakeResponse(200, {"safe": True}),
                        lambda _, error=control: (_ for _ in ()).throw(error),
                    )

    def test_exception_chain_uses_types_without_message_inspection(self):
        try:
            raise socket.gaierror("SECRET_MARKER")
        except socket.gaierror as inner:
            try:
                raise RuntimeError("SECRET_MARKER") from inner
            except RuntimeError as wrapped:
                result = self.run_once(wrapped)
        self.assertEqual(result.failure_class, outcome.FailureClass.DNS)
        self.assertEqual(result.request_reached_server, outcome.ReachedServer.NO)

    def test_exception_chain_is_bounded_and_context_cannot_override(self):
        current = socket.gaierror("SECRET_MARKER")
        for _ in range(7):
            try:
                raise RuntimeError("SECRET_MARKER") from current
            except RuntimeError as wrapped:
                current = wrapped
        result = self.run_once(current)
        self.assertEqual(result.failure_class, outcome.FailureClass.DNS)

        current = socket.gaierror("SECRET_MARKER")
        for _ in range(8):
            try:
                raise RuntimeError("SECRET_MARKER") from current
            except RuntimeError as wrapped:
                current = wrapped
        result = self.run_once(current)
        self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)

        if requests is not None:
            try:
                raise ssl.SSLError("SECRET_MARKER")
            except ssl.SSLError:
                try:
                    raise requests.exceptions.ReadTimeout("SECRET_MARKER")
                except requests.exceptions.ReadTimeout as wrapped:
                    result = self.run_once(wrapped)
            self.assertEqual(result.failure_class, outcome.FailureClass.TIMEOUT)
            self.assertEqual(result.request_reached_server, outcome.ReachedServer.UNKNOWN)

            try:
                raise ssl.SSLError("SECRET_MARKER")
            except ssl.SSLError:
                try:
                    raise requests.exceptions.ConnectionError("SECRET_MARKER")
                except requests.exceptions.ConnectionError as wrapped:
                    result = self.run_once(wrapped)
            self.assertEqual(result.failure_class, outcome.FailureClass.CONNECT)
            self.assertEqual(result.request_reached_server, outcome.ReachedServer.UNKNOWN)

    def test_suppressed_context_is_not_followed(self):
        try:
            try:
                raise socket.gaierror("SECRET_MARKER")
            except socket.gaierror:
                raise RuntimeError("SECRET_MARKER") from None
        except RuntimeError as wrapped:
            result = self.run_once(wrapped)
        self.assertEqual(result.failure_class, outcome.FailureClass.UNEXPECTED)
        self.assertEqual(result.request_reached_server, outcome.ReachedServer.UNKNOWN)

    def test_send_once_failure_still_calls_once(self):
        calls = 0

        def send_once():
            nonlocal calls
            calls += 1
            raise RuntimeError("SECRET_MARKER")

        result = outcome.probe_once(send_once)
        self.assertEqual(calls, 1)
        self.assertEqual(result.request_count, 1)

    def test_static_classifier_has_no_transport_or_secret_surface(self):
        source = Path(outcome.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\bendpoint\b|\bcredential\w*\b|\btoken\w*\b", source, re.IGNORECASE))
        self.assertIsNone(re.search(r"\bGET\b|\bPOST\b", source))
        self.assertNotIn("SECRET_MARKER", source)

        tree = ast.parse(source)
        forbidden_calls = {
            "get",
            "post",
            "request",
            "Session",
            "urlopen",
            "urlretrieve",
            "HTTPConnection",
            "HTTPSConnection",
            "build_opener",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_calls)
            if isinstance(node, ast.ExceptHandler):
                self.assertFalse(isinstance(node.type, ast.Name) and node.type.id == "BaseException")
            if isinstance(node, ast.ImportFrom):
                self.assertFalse(node.module and (node.module == "http" or node.module.startswith("http.") or node.module == "urllib" or node.module.startswith("urllib.")))


if __name__ == "__main__":
    unittest.main()
