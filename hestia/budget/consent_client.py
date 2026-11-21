"""Bounded AIS consent control plane; never retries an uncertain POST.

The provider contract is https://enablebanking.com/docs/api/reference/ .
The service pins the registered redirect, configured bank, consent lifetime and
trusted foreground request context. This client accepts no arbitrary headers,
provider origin, credentials, payment operation or acquisition endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import http.client
import ipaddress
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from .client import (API_ORIGIN, TIMEOUT, BudgetAPIError, EnableBankingClient,
                     _UUID, _http_error, _reject_constant, _safe_deadline, _unique_error_fields)


MAX_REQUEST = 8192
_SESSION = re.compile(rf"/sessions/{_UUID}", re.ASCII)
_DATE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})", re.ASCII)
_PSU_FIELDS = (("ip_address", "Psu-Ip-Address", 64), ("user_agent", "Psu-User-Agent", 1024),
               ("referer", "Psu-Referer", 2048), ("accept", "Psu-Accept", 1024),
               ("accept_charset", "Psu-Accept-Charset", 256), ("accept_encoding", "Psu-Accept-Encoding", 256),
               ("accept_language", "Psu-Accept-Language", 256), ("geo_location", "Psu-Geo-Location", 64))


def _text(value, maximum, *, ascii_only=False):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or (ascii_only and not value.isascii())):
        raise BudgetAPIError(code="invalid_request")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise ValueError
    except ValueError:
        raise BudgetAPIError(code="invalid_request") from None
    return value


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise BudgetAPIError(code="invalid_request")
    return value


def _https_url(value):
    value = _text(value, 2048, ascii_only=True)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or any(char.isspace() or char == "\\" for char in value)):
            raise ValueError
    except ValueError:
        raise BudgetAPIError(code="invalid_request") from None
    return value


@dataclass(frozen=True, repr=False)
class PSUContext:
    """Server-created foreground context, never parsed from an arbitrary header map.

    The HTTP service must establish the real peer using its trusted proxy policy.
    None means unavailable; bank requirements must fail closed in the service.
    Values are neither persisted nor added to background acquisition requests.
    """

    ip_address: str | None = None
    user_agent: str | None = None
    referer: str | None = None
    accept: str | None = None
    accept_charset: str | None = None
    accept_encoding: str | None = None
    accept_language: str | None = None
    geo_location: str | None = None

    def __post_init__(self):
        for name, _, maximum in _PSU_FIELDS:
            value = getattr(self, name)
            if value is not None:
                _text(value, maximum, ascii_only=True)
        if self.ip_address is not None:
            try:
                if "%" in self.ip_address:
                    raise ValueError
                ipaddress.ip_address(self.ip_address)
            except ValueError:
                raise BudgetAPIError(code="invalid_request") from None
        if self.referer is not None:
            _https_url(self.referer)
        if self.geo_location is not None:
            _geo_location(self.geo_location)

    def headers(self):
        return {header: getattr(self, name) for name, header, _ in _PSU_FIELDS if getattr(self, name) is not None}


def _geo_location(value):
    if not re.fullmatch(r"-?[0-9]{1,3}(?:\.[0-9]{1,8})?,-?[0-9]{1,3}(?:\.[0-9]{1,8})?", value, re.ASCII):
        raise BudgetAPIError(code="invalid_request")
    latitude, longitude = map(Decimal, value.split(","))
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise BudgetAPIError(code="invalid_request")


def _route(method, path):
    if not isinstance(path, str):
        raise BudgetAPIError(code="invalid_request")
    if (method, path) in {("GET", "/aspsps"), ("POST", "/auth"), ("POST", "/sessions")}:
        return
    if method in {"GET", "DELETE"} and _SESSION.fullmatch(path):
        return
    raise BudgetAPIError(code="invalid_request")


def _country(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{2}", value, re.ASCII):
        raise BudgetAPIError(code="invalid_request")
    return value


def _query(path, query):
    query = {} if query is None else _object(query, set(), {"country", "psu_type", "service"})
    if query and path != "/aspsps":
        raise BudgetAPIError(code="invalid_request")
    if "country" in query:
        _country(query["country"])
    if ("psu_type" in query and query["psu_type"] != "personal") or ("service" in query and query["service"] != "AIS"):
        raise BudgetAPIError(code="invalid_request")
    return urlencode(query)


def _valid_until(value, now):
    value = _text(value, 64, ascii_only=True)
    try:
        if not _DATE_TIME.fullmatch(value):
            raise ValueError
        stamp = datetime.fromisoformat(value).timestamp()
        if stamp <= now:
            raise ValueError
    except (ValueError, OverflowError, OSError):
        raise BudgetAPIError(code="invalid_request") from None
    return value


def _access(value, now):
    flags = {"balances", "transactions", "transactions_extended_history"}
    value = _object(value, {"valid_until"}, flags)
    result = {"valid_until": _valid_until(value["valid_until"], now)}
    for name in flags & value.keys():
        if type(value[name]) is not bool:
            raise BudgetAPIError(code="invalid_request")
        result[name] = value[name]
    return result


def _auth_body(body, now):
    value = _object(body, {"access", "aspsp", "state", "redirect_url", "psu_type"}, {"auth_method", "language"})
    bank = _object(value["aspsp"], {"name", "country"})
    if value["psu_type"] != "personal":
        raise BudgetAPIError(code="invalid_request")
    result = {"access": _access(value["access"], now),
              "aspsp": {"name": _text(bank["name"], 128), "country": _country(bank["country"])},
              "state": _text(value["state"], 1024, ascii_only=True),
              "redirect_url": _https_url(value["redirect_url"]), "psu_type": "personal"}
    if "auth_method" in value:
        result["auth_method"] = _text(value["auth_method"], 128)
    if "language" in value:
        if not isinstance(value["language"], str) or not re.fullmatch(r"[a-z]{2}", value["language"], re.ASCII):
            raise BudgetAPIError(code="invalid_request")
        result["language"] = value["language"]
    return result


def _response_socket(response):
    # urllib HTTPResponse -> fp/raw/_sock; HTTPError adds one fp wrapper.
    # Injected in-memory responses need no socket. Bound traversal even for an
    # invalid/cyclic wrapper, and never inspect or expose its URL or headers.
    node = response
    for _ in range(5):
        if callable(getattr(node, "settimeout", None)):
            return node
        following = getattr(node, "fp", None)
        if following is None:
            following = getattr(node, "raw", None)
        if following is None:
            following = getattr(node, "_sock", None)
        if following is None:
            return None
        node = following
    raise BudgetAPIError(code="invalid_response")


class _DeadlineReader:
    def __init__(self, client, response, deadline):
        self.client, self.deadline = client, deadline
        self.response = response
        self.socket = _response_socket(response)
        reader = getattr(response, "read1", None)
        self.reader = reader if callable(reader) else response.read

    def read1(self, size):
        # Content-Length completion closes HTTPResponse.fp and its socket before
        # the base reader's final EOF probe. Never reuse that closed socket.
        self.socket = _response_socket(self.response)
        if self.socket is not None:
            try:
                self.socket.settimeout(self.client._remaining(self.deadline))
            except (OSError, ValueError, TypeError):
                raise BudgetAPIError(code="network", retryable=True) from None
        return self.reader(size)


class EnableBankingConsentClient(EnableBankingClient):
    """Fixed-origin control-plane calls using the acquisition client's bounded primitives."""

    def _read_body(self, response, maximum, deadline):
        return super()._read_body(_DeadlineReader(self, response, deadline), maximum, deadline)

    def _deadline(self, deadline):
        try:
            now = self._monotonic()
        except (ValueError, TypeError, OverflowError):
            raise BudgetAPIError(code="invalid_configuration") from None
        if _safe_deadline(now) is None or now + TIMEOUT <= now:
            raise BudgetAPIError(code="invalid_configuration")
        if deadline is not None and _safe_deadline(deadline) is None:
            raise BudgetAPIError(code="invalid_request")
        bounded = now + TIMEOUT if deadline is None else min(now + TIMEOUT, deadline)
        self._remaining(bounded)
        return bounded

    def _payload(self, path, body):
        if path == "/auth":
            try:
                now = self._clock()
            except (ValueError, TypeError, OverflowError):
                raise BudgetAPIError(code="invalid_configuration") from None
            if _safe_deadline(now) is None:
                raise BudgetAPIError(code="invalid_configuration")
            value = _auth_body(body, now)
        else:
            value = _object(body, {"code"})
            value = {"code": _text(value["code"], 4096)}
        data = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_REQUEST:
            raise BudgetAPIError(code="invalid_request")
        return data

    def _open(self, request, path, deadline):
        try:
            return self._opener.open(request, timeout=self._remaining(deadline))
        except HTTPError as exc:
            try:
                if exc.code != 429:
                    self._remaining(deadline)
                error = _http_error(exc.code, response=exc, path=path, clock=self._clock,
                                    read_body=lambda response, maximum: self._read_body(response, maximum, deadline))
            finally:
                try:
                    exc.close()
                except (OSError, http.client.HTTPException):
                    pass
            raise error from None
        except (OSError, URLError, ValueError, http.client.HTTPException):
            self._remaining(deadline)
            raise BudgetAPIError(code="network", retryable=True) from None

    def _request(self, method, path, query=None, body=None, *, deadline=None, psu=None):
        _route(method, path)
        encoded = _query(path, query)
        if psu is not None and type(psu) is not PSUContext:
            raise BudgetAPIError(code="invalid_request")
        data = self._payload(path, body) if method == "POST" else None
        deadline = self._deadline(deadline)
        headers = {"Authorization": "Bearer " + self._jwt(), "Accept": "application/json",
                   "User-Agent": "hestia-budget/1"}
        if psu is not None:
            headers.update(psu.headers())
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(API_ORIGIN + path + ("?" + encoded if encoded else ""), data=data,
                          method=method, headers=headers)
        response = self._open(request, path, deadline)
        raw, status = self._response_body(response, path, deadline)
        try:
            result = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                                object_pairs_hook=_unique_error_fields)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise BudgetAPIError(status, "invalid_response") from None
        self._remaining(deadline)
        return result

    def get(self, path, query=None, *, deadline=None, psu=None):
        return self._request("GET", path, query=query, deadline=deadline, psu=psu)

    def post(self, path, body, *, deadline=None, psu=None):
        return self._request("POST", path, body=body, deadline=deadline, psu=psu)

    def delete(self, path, *, deadline=None, psu=None):
        return self._request("DELETE", path, deadline=deadline, psu=psu)
