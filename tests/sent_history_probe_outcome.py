"""Pure in-memory classification after an in-process ``send_once`` attempt.

This classifier covers only an attempt whose injected callable has already
been invoked inside the process. Refusal by an outer sandbox or tool before
process startup is outside this classifier.
"""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

try:
    import requests as _requests
except Exception:  # pragma: no cover - optional dependency
    _requests = None

try:
    import urllib3 as _urllib3
except Exception:  # pragma: no cover - optional dependency
    _urllib3 = None


class FailureClass(str, Enum):
    SUCCESS = "SUCCESS"
    DNS = "DNS"
    CONNECT = "CONNECT"
    TLS = "TLS"
    TIMEOUT = "TIMEOUT"
    HTTP_AUTH = "HTTP_AUTH"
    HTTP_OTHER = "HTTP_OTHER"
    REDIRECT = "REDIRECT"
    JSON = "JSON"
    SCHEMA = "SCHEMA"
    LOCAL_PERMISSION = "LOCAL_PERMISSION"
    UNEXPECTED = "UNEXPECTED"


class ReachedServer(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class HttpStatusClass(str, Enum):
    NONE = "NONE"
    S2XX = "2XX"
    S3XX = "3XX"
    S4XX = "4XX"
    S5XX = "5XX"
    OTHER = "OTHER"


class TLSSubtype(str, Enum):
    """Finite, message-free detail for a TLS-class transport failure."""

    NONE = "NONE"
    CERT_VERIFY = "CERT_VERIFY"
    EOF = "EOF"
    ZERO_RETURN = "ZERO_RETURN"
    SYSCALL = "SYSCALL"
    PROTOCOL_OR_HANDSHAKE = "PROTOCOL_OR_HANDSHAKE"
    UNKNOWN = "UNKNOWN"


class SchemaValidationError(Exception):
    """Safe signal for a payload that does not satisfy the expected shape."""


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    failure_class: FailureClass
    request_reached_server: ReachedServer
    http_status_class: HttpStatusClass
    json_parsed: bool
    schema_valid: bool | None
    _payload: Any = field(default=None, repr=False, compare=False)
    tls_subtype: TLSSubtype = field(default=TLSSubtype.NONE, kw_only=True)
    tls_verify_code: int | None = field(default=None, kw_only=True)
    request_count: int = field(default=1, init=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "request_reached_server": self.request_reached_server.value,
            "http_status_class": self.http_status_class.value,
            "request_count": 1,
            "json_parsed": self.json_parsed,
            "schema_valid": self.schema_valid,
            "tls_subtype": self.tls_subtype.value,
            "tls_verify_code": self.tls_verify_code,
        }

    def public_json(self) -> str:
        return json.dumps(self.public_dict(), sort_keys=True, separators=(",", ":"))

    def get_payload(self) -> Any:
        if self.failure_class is not FailureClass.SUCCESS:
            raise LookupError("payload is available only for a successful outcome")
        return self._payload


def _request_exception_types(name: str) -> tuple[type[BaseException], ...]:
    if _requests is None:
        return ()
    value = getattr(getattr(_requests, "exceptions", None), name, None)
    return (value,) if isinstance(value, type) else ()


_REQUESTS_SSL = _request_exception_types("SSLError")
_REQUESTS_CONNECT_TIMEOUT = _request_exception_types("ConnectTimeout")
_REQUESTS_READ_TIMEOUT = _request_exception_types("ReadTimeout")
_REQUESTS_TIMEOUT = _request_exception_types("Timeout")
_REQUESTS_CONNECTION = _request_exception_types("ConnectionError")
_REQUESTS_REQUEST = _request_exception_types("RequestException")


def _nested_exception_type(module: Any, *path: str) -> tuple[type[BaseException], ...]:
    current = module
    for part in path:
        current = getattr(current, part, None)
        if current is None:
            return ()
    return (current,) if isinstance(current, type) else ()


_URLLIB3_SSL = _nested_exception_type(_urllib3, "exceptions", "SSLError")


def _optional_ssl_types(*names: str) -> tuple[type[BaseException], ...]:
    values: list[type[BaseException]] = []
    for name in names:
        value = getattr(ssl, name, None)
        if isinstance(value, type) and issubclass(value, BaseException):
            values.append(value)
    return tuple(values)


_SSL_CERT_VERIFY = _optional_ssl_types("SSLCertVerificationError")
_SSL_EOF = _optional_ssl_types("SSLEOFError")
_SSL_ZERO_RETURN = _optional_ssl_types("SSLZeroReturnError")
_SSL_SYSCALL = _optional_ssl_types("SSLSyscallError")
_SSL_PROTOCOL_OR_HANDSHAKE = _optional_ssl_types(
    "SSLProtocolError", "SSLHandshakeError"
)

_MAX_EXCEPTION_DEPTH = 8
_MAX_EXCEPTION_NODES = 32
_MAX_EXCEPTION_ARGS = 8


def _exception_class(exc: BaseException) -> FailureClass:
    if isinstance(exc, PermissionError):
        return FailureClass.LOCAL_PERMISSION
    if isinstance(exc, socket.gaierror):
        return FailureClass.DNS
    if (
        isinstance(exc, _REQUESTS_SSL)
        or isinstance(exc, _URLLIB3_SSL)
        or isinstance(exc, ssl.SSLError)
    ):
        return FailureClass.TLS
    if isinstance(exc, _REQUESTS_CONNECT_TIMEOUT):
        return FailureClass.CONNECT
    if isinstance(exc, _REQUESTS_READ_TIMEOUT):
        return FailureClass.TIMEOUT
    if isinstance(exc, _REQUESTS_TIMEOUT) or isinstance(exc, TimeoutError):
        return FailureClass.TIMEOUT
    if isinstance(exc, _REQUESTS_CONNECTION):
        return FailureClass.CONNECT
    if isinstance(exc, _REQUESTS_REQUEST):
        return FailureClass.UNEXPECTED
    return FailureClass.UNEXPECTED


def _exception_args(exc: BaseException) -> tuple[BaseException, ...]:
    """Return only bounded exception objects from ``args``; never inspect text."""

    try:
        args = exc.args
    except Exception:
        return ()
    if not isinstance(args, tuple):
        return ()

    found: list[BaseException] = []
    pending: list[object] = list(args[:_MAX_EXCEPTION_ARGS])
    inspected = 0
    while pending and inspected < _MAX_EXCEPTION_ARGS:
        value = pending.pop(0)
        inspected += 1
        if isinstance(value, BaseException):
            found.append(value)
        elif isinstance(value, (tuple, list)):
            pending.extend(value[:_MAX_EXCEPTION_ARGS])
    return tuple(found)


def _exception_links(exc: BaseException) -> tuple[BaseException, ...]:
    links: list[BaseException] = []
    cause = exc.__cause__
    if cause is not None:
        links.append(cause)
    elif not exc.__suppress_context__:
        context = exc.__context__
        if context is not None:
            links.append(context)
    links.extend(_exception_args(exc))
    return tuple(links)


def _walk_exception_objects(exc: BaseException):
    """Walk only exception objects with bounded depth/nodes and no text access."""

    seen: set[int] = set()
    visited = 0

    def visit(current: BaseException, depth: int):
        nonlocal visited
        if depth >= _MAX_EXCEPTION_DEPTH or visited >= _MAX_EXCEPTION_NODES:
            return
        if id(current) in seen:
            return
        seen.add(id(current))
        visited += 1
        yield current
        for linked in _exception_links(current):
            yield from visit(linked, depth + 1)

    yield from visit(exc, 0)


def _tls_subtype(exc: BaseException) -> TLSSubtype:
    if isinstance(exc, _SSL_CERT_VERIFY):
        return TLSSubtype.CERT_VERIFY
    if isinstance(exc, _SSL_EOF):
        return TLSSubtype.EOF
    if isinstance(exc, _SSL_ZERO_RETURN):
        return TLSSubtype.ZERO_RETURN
    if isinstance(exc, _SSL_SYSCALL):
        return TLSSubtype.SYSCALL
    if isinstance(exc, _SSL_PROTOCOL_OR_HANDSHAKE):
        return TLSSubtype.PROTOCOL_OR_HANDSHAKE
    if _exception_class(exc) is FailureClass.TLS:
        return TLSSubtype.UNKNOWN
    return TLSSubtype.NONE


def _tls_verify_code(exc: BaseException) -> tuple[int | None, bool]:
    if not isinstance(exc, ssl.SSLCertVerificationError):
        return None, False
    try:
        value = exc.verify_code
    except Exception:
        return None, True
    if type(value) is int and 1 <= value <= 255:
        return value, False
    return None, True


def _classify_exception_details(
    exc: BaseException,
) -> tuple[FailureClass, TLSSubtype, int | None]:
    """Classify by type only, following bounded cause/context/args links."""

    failure = FailureClass.UNEXPECTED
    specific: set[TLSSubtype] = set()
    verify_codes: set[int] = set()
    verify_code_invalid = False
    saw_tls = False
    for current in _walk_exception_objects(exc):
        candidate = _exception_class(current)
        if failure is FailureClass.UNEXPECTED and candidate is not FailureClass.UNEXPECTED:
            failure = candidate
        if candidate is FailureClass.TLS:
            saw_tls = True
            subtype = _tls_subtype(current)
            if subtype not in (TLSSubtype.NONE, TLSSubtype.UNKNOWN):
                specific.add(subtype)
            code, invalid = _tls_verify_code(current)
            verify_code_invalid = verify_code_invalid or invalid
            if code is not None:
                verify_codes.add(code)

    if failure is not FailureClass.TLS:
        return failure, TLSSubtype.NONE, None
    subtype = TLSSubtype.NONE
    if len(specific) == 1:
        subtype = next(iter(specific))
    else:
        subtype = TLSSubtype.UNKNOWN if saw_tls else TLSSubtype.NONE
    verify_code = (
        next(iter(verify_codes))
        if len(verify_codes) == 1 and not verify_code_invalid
        else None
    )
    if subtype is not TLSSubtype.CERT_VERIFY:
        verify_code = None
    return failure, subtype, verify_code


def _classify_exception(exc: BaseException) -> FailureClass:
    return _classify_exception_details(exc)[0]


def _response_failure() -> ProbeOutcome:
    return ProbeOutcome(
        FailureClass.UNEXPECTED,
        ReachedServer.UNKNOWN,
        HttpStatusClass.NONE,
        json_parsed=False,
        schema_valid=None,
    )


def _status_class(status_code: Any) -> HttpStatusClass:
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise TypeError("status code must be an integer")
    if 200 <= status_code < 300:
        return HttpStatusClass.S2XX
    if 300 <= status_code < 400:
        return HttpStatusClass.S3XX
    if 400 <= status_code < 500:
        return HttpStatusClass.S4XX
    if 500 <= status_code < 600:
        return HttpStatusClass.S5XX
    return HttpStatusClass.OTHER


def _network_failure(
    failure: FailureClass, tls_subtype: TLSSubtype = TLSSubtype.NONE
) -> ProbeOutcome:
    reached = ReachedServer.NO if failure in (
        FailureClass.DNS,
        FailureClass.LOCAL_PERMISSION,
    ) else ReachedServer.UNKNOWN
    return ProbeOutcome(
        failure,
        reached,
        HttpStatusClass.NONE,
        json_parsed=False,
        schema_valid=None,
        tls_subtype=tls_subtype if failure is FailureClass.TLS else TLSSubtype.NONE,
    )


def probe_once(
    send_once: Callable[[], Any],
    schema_validator: Callable[[Any], bool] | None = None,
) -> ProbeOutcome:
    """Call the injected operation exactly once and classify its outcome."""
    try:
        response = send_once()
    except Exception as exc:
        failure, tls_subtype, tls_verify_code = _classify_exception_details(exc)
        result = _network_failure(failure, tls_subtype)
        if tls_verify_code is None:
            return result
        return ProbeOutcome(
            result.failure_class,
            result.request_reached_server,
            result.http_status_class,
            result.json_parsed,
            result.schema_valid,
            tls_subtype=tls_subtype,
            tls_verify_code=tls_verify_code,
        )

    try:
        status_code = response.status_code
        status_class = _status_class(status_code)
    except Exception:
        return _response_failure()

    if status_class is HttpStatusClass.S3XX:
        return ProbeOutcome(
            FailureClass.REDIRECT,
            ReachedServer.YES,
            status_class,
            json_parsed=False,
            schema_valid=None,
        )
    if status_class is not HttpStatusClass.S2XX:
        failure = FailureClass.HTTP_AUTH if status_code in (401, 403) else FailureClass.HTTP_OTHER
        return ProbeOutcome(
            failure,
            ReachedServer.YES,
            status_class,
            json_parsed=False,
            schema_valid=None,
        )

    try:
        payload = response.json()
    except Exception:
        return ProbeOutcome(
            FailureClass.JSON,
            ReachedServer.YES,
            status_class,
            json_parsed=False,
            schema_valid=None,
        )

    if schema_validator is not None:
        try:
            valid = schema_validator(payload)
        except SchemaValidationError:
            valid = False
        except Exception:
            return ProbeOutcome(
                FailureClass.UNEXPECTED,
                ReachedServer.YES,
                status_class,
                json_parsed=True,
                schema_valid=None,
            )
        if type(valid) is not bool:
            return ProbeOutcome(
                FailureClass.UNEXPECTED,
                ReachedServer.YES,
                status_class,
                json_parsed=True,
                schema_valid=None,
            )
        if not valid:
            return ProbeOutcome(
                FailureClass.SCHEMA,
                ReachedServer.YES,
                status_class,
                json_parsed=True,
                schema_valid=False,
            )

    return ProbeOutcome(
        FailureClass.SUCCESS,
        ReachedServer.YES,
        status_class,
        json_parsed=True,
        schema_valid=True if schema_validator is not None else None,
        _payload=payload,
    )
