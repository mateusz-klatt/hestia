"""Default-off owner receipts and device pairing routes."""
from __future__ import annotations

import asyncio
import os
import re
import time

from aiohttp import web
from pydantic import ValidationError

from .. import api_contract, authz, store_sql
from ..db import init_db
from . import api, consent_context, device_token, gmail_config
from .analytics_core import canonical_money
from .config import BudgetConfigError
from .receipt_parser import ReceiptError
from .receipt_store import BudgetReceiptStore
from .receipt_transaction_search import MANUAL_LINK_VERSION, ReceiptTransactionSearch
from .views import BudgetUsersBackendRequired

PREFIX = "/api/budget/receipts"
FLAG = "HESTIA_BUDGET_RECEIPTS_ENABLED"
READS = ("detail", "image", "group/detail")
_TX_SEARCH = "transaction-search"
_ATTACH_EVIDENCE = "attach-evidence"
WRITES = {
    "parse-text": (api_contract.BudgetReceiptParseTextRequest, 49152),
    "confirm": (api_contract.BudgetReceiptConfirmRequest, 131072),
    "list": (api_contract.BudgetReceiptListRequest, 16384),
    "search": (api_contract.BudgetReceiptSearchRequest, 16384),
    _TX_SEARCH: (api_contract.BudgetReceiptTransactionSearchRequest, 16384),
    "delete": (api_contract.BudgetReceiptDeleteRequest, 16384),
    _ATTACH_EVIDENCE: (api_contract.BudgetReceiptAttachEvidenceRequest, 16384),
    "group/candidates": (api_contract.BudgetReceiptGroupCandidatesRequest, 16384),
    "group/link": (api_contract.BudgetReceiptGroupLinkRequest, 16384),
    "group/unlink": (api_contract.BudgetReceiptGroupUnlinkRequest, 16384),
}
_GROUP_OPERATIONS = frozenset({"group/detail", "group/candidates", "group/link", "group/unlink"})
_CONTRACT_HEADER = "X-Hestia-Contract-Version"
_CONTRACT_VERSION = re.compile(r"0\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\Z")
_NATIVE_GMAIL_VERSION = (0, 40, 0)
_GATEWAY_EVIDENCE_VERSION = (0, 52, 0)
_ID_MATCH_VERSION = (0, 57, 0)
_MANY_TO_ONE_VERSION = (0, 59, 0)
_LINK_INDICATORS_VERSION = (0, 60, 0)
_DISPLAY_SHOP_NAME_VERSION = (0, 61, 0)
_MULTI_STATUS_VERSION = (0, 65, 0)
_EVIDENCE_TARGET_VERSION = (0, 69, 0)
_CURRENT_VERSION = tuple(int(part) for part in api_contract.CONTRACT_VERSION.split("."))
_RECEIPT_RESULT_KEY = {"parse-text": "draft", "confirm": "receipt", "list": "receipts",
                       "search": "receipts", "detail": "receipt"}
_LEGACY_SOURCE = {"paste": "live_text", "email": "live_text", "photo": "local_ocr", "attachment": "local_ocr"}
_GMAIL_FIELDS = ("document_kind", "invoice_number", "due_date", "import_expires_at",
                 "forwarded_manually", "forwarded_sender_domain")
_GATEWAY_EVIDENCE_FIELDS = ("gateway", "gateway_status", "fee_amount", "payee_host",
                            "evidence_for_receipt_id", "has_document", "payment_evidence")


def enabled():
    return os.environ.get(FLAG) == "1"


_RETENTION_SECRET = b"hestia/budget/receipts/retention/v1"


def _store(secret, device_id=None, actor=None, *, store_type=BudgetReceiptStore):
    engine, sessions = init_db()
    with sessions() as session:
        store_sql.auth_session_phase(session)
        authz.require_supported_policy_phase(session)
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return store_type(
        sessions, clock=time.time, cursor_secret=secret, device_id=device_id, actor=actor,
        ref_hmac_secret=gmail_config.load_ref_hmac_secret(session_secret=secret),
        card_accounts=gmail_config.load_card_accounts(),
    )


def retain_expired(path=None, clock=time.time):
    """Purge expired drafts and raw text without the route flag or a browser secret.

    A constant non-session key satisfies the store constructor. The sweep never
    reads or writes a cursor, and it skips a database that is not the users backend.
    """
    engine, sessions = init_db(path)
    with sessions() as session:
        store_sql.auth_session_phase(session)
        authz.require_supported_policy_phase(session)
    if not store_sql.is_users_db_authoritative(engine):
        return {"drafts_purged": 0, "raw_texts_purged": 0}
    return BudgetReceiptStore(sessions, clock=clock, cursor_secret=_RETENTION_SECRET).purge_expired()


def _receipt_id(request, *, field="receipt_id"):
    if set(request.query) != {field} or len(request.query.getall(field)) != 1:
        raise ReceiptError()
    return request.query[field]


def _run(owner, operation, values, secret, device_id, actor=None, *,
         include_linked_transaction=False, include_display_shop_name=False):
    if operation == _TX_SEARCH:
        return _store(secret, actor=actor, store_type=ReceiptTransactionSearch).search_transactions(owner, values)
    if operation == "confirm" and values["link"] is not None and "transaction_id" in values["link"]:
        return _store(secret, actor=actor, store_type=ReceiptTransactionSearch).confirm_manual(owner, values)
    store = _store(secret, device_id, actor)
    if operation == "parse-text":
        return store.parse_text(owner, values["text"], source_kind=values["source_kind"])
    if operation == _ATTACH_EVIDENCE:
        return store.attach_evidence(owner, values)
    if operation in ("list", "search"):
        return getattr(store, operation)(
            owner, values, include_linked_transaction=include_linked_transaction,
            include_display_shop_name=include_display_shop_name,
        )
    return getattr(store, operation.replace("/", "_"))(owner, values)


def _device_error(status, code, retry=None):
    return device_token.json_error({"status": status, "code": code, "retry": retry})


_DEVICE_BODY = 4096
_DEVICE_BROWSER_REQUESTS = {
    "pair-code": api_contract.BudgetReceiptDeviceBrowserRequest,
    "pair-code-cancel": api_contract.BudgetReceiptDeviceCancelRequest,
    "list": api_contract.BudgetReceiptDeviceBrowserRequest,
    "revoke": api_contract.BudgetReceiptDeviceRevokeRequest,
}


def _receipt_contract(request, operation):
    if (operation not in _RECEIPT_RESULT_KEY and operation not in ("gmail-status", _TX_SEARCH, _ATTACH_EVIDENCE)
            and operation not in _GROUP_OPERATIONS):
        return (0, 0, 0)
    values = request.headers.getall(_CONTRACT_HEADER, [])
    if not values:
        return (0, 0, 0)
    if len(values) != 1 or not _CONTRACT_VERSION.fullmatch(values[0]):
        raise api._Rejected(400, "budget_receipt_contract_version_invalid")
    version = tuple(int(part) for part in values[0].split("."))
    if version > _CURRENT_VERSION:
        raise api._Rejected(400, "budget_receipt_contract_version_invalid")
    return version


def _response(operation, result, version):
    if version < _DISPLAY_SHOP_NAME_VERSION and operation in _RECEIPT_RESULT_KEY:
        key = _RECEIPT_RESULT_KEY[operation]
        receipts = result[key] if key == "receipts" else (result[key],)
        for receipt in receipts:
            receipt.pop("display_shop_name", None)
    if version < _LINK_INDICATORS_VERSION and operation in _RECEIPT_RESULT_KEY:
        key = _RECEIPT_RESULT_KEY[operation]
        receipts = result[key] if key == "receipts" else (result[key],)
        for receipt in receipts:
            receipt.pop("linked_transaction", None)
    if version < _MANY_TO_ONE_VERSION and operation in _RECEIPT_RESULT_KEY:
        key = _RECEIPT_RESULT_KEY[operation]
        receipts = result[key] if key == "receipts" else (result[key],)
        for receipt in receipts:
            receipt.pop("link_group", None)
        result.pop("group_candidates", None)
    if version < _ID_MATCH_VERSION and operation in _RECEIPT_RESULT_KEY:
        key = _RECEIPT_RESULT_KEY[operation]
        receipts = result[key] if key == "receipts" else (result[key],)
        for receipt in receipts:
            receipt.pop("card_last4", None)
        result.pop("no_bank_data", None)
        candidates = result.get("candidates")
        if candidates:
            expected_total = canonical_money(result[key]["total_amount"])
            filtered = []
            for candidate in candidates:
                if canonical_money(candidate["amount"]) != expected_total:
                    continue
                if candidate.get("match_strength") == "id":
                    candidate["match_strength"] = "none"
                filtered.append(candidate)
            result["candidates"] = filtered
    if version < _GATEWAY_EVIDENCE_VERSION and operation in _RECEIPT_RESULT_KEY:
        key = _RECEIPT_RESULT_KEY[operation]
        receipts = result[key] if key == "receipts" else (result[key],)
        for receipt in receipts:
            for field in _GATEWAY_EVIDENCE_FIELDS:
                receipt.pop(field, None)
            if version < _NATIVE_GMAIL_VERSION:
                if receipt["status"] == "imported":
                    receipt["status"] = "draft"
                receipt["source_kind"] = _LEGACY_SOURCE.get(receipt["source_kind"], receipt["source_kind"])
                for field in _GMAIL_FIELDS:
                    receipt.pop(field, None)
    return web.json_response(result, headers=api.PRIVATE_HEADERS)


def register(app, read_json_body, *, auth_enabled, session_secret, login_limiter):
    async def handle(request):
        device_id = request.get(device_token.REQUEST_KEY)
        operation = request.path.removeprefix(PREFIX + "/")
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            actor = request.get(device_token.ACTOR_REQUEST_KEY, owner) if device_id is not None else owner
            if request.method == "GET":
                version = _receipt_contract(request, operation)
                if operation == "group/detail" and version < _MANY_TO_ONE_VERSION:
                    raise ReceiptError("budget_receipt_not_found", 404)
                identifier = _receipt_id(request, field="group_id" if operation == "group/detail" else "receipt_id")
                result = await api._settled_thread(
                    lambda: _run_read(
                        owner, operation, identifier, secret, device_id, actor,
                        include_display_shop_name=version >= _DISPLAY_SHOP_NAME_VERSION,
                        include_evidence_target=version >= _EVIDENCE_TARGET_VERSION,
                    ),
                )
                return _response(operation, result, version)
            if device_id is None:
                api._browser(request, owner, secret)
            elif operation == "parse-text":
                retry = device_token.parse_limited(device_id, time.monotonic())
                if retry is not None:
                    return _device_error(429, "device_token_rate_limited", retry)
            version = _receipt_contract(request, operation)
            if ((operation in _GROUP_OPERATIONS and version < _MANY_TO_ONE_VERSION)
                    or (operation == _ATTACH_EVIDENCE and version < _EVIDENCE_TARGET_VERSION)):
                raise ReceiptError("budget_receipt_not_found", 404)
            if operation == _TX_SEARCH and version < MANUAL_LINK_VERSION:
                raise ReceiptError("budget_receipt_not_found", 404)
            body, error = await read_json_body(request, WRITES[operation][1], reject_duplicates=True)
            if error:
                return body
            if (operation == "list" and version < _MULTI_STATUS_VERSION
                    and isinstance(body, dict) and "statuses" in body):
                raise ReceiptError()
            validated = WRITES[operation][0].model_validate(body)
            if operation == "confirm" and isinstance(validated.link, api_contract.BudgetReceiptManualLinkRequest):
                if version < MANUAL_LINK_VERSION:
                    raise ReceiptError()
                if device_id is not None:
                    raise api._Rejected(403, "budget_browser_session_required")
            values = validated.model_dump(exclude_unset=operation in ("confirm", "list", _TX_SEARCH))
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if device_id is None:
                api._browser(request, owner, secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            actor = request.get(device_token.ACTOR_REQUEST_KEY, owner) if device_id is not None else owner
            if operation in ("list", "search") and version >= _LINK_INDICATORS_VERSION:
                result = await api._settled_thread(
                    lambda: _run(
                        owner, operation, values, secret, device_id, actor, include_linked_transaction=True,
                        include_display_shop_name=version >= _DISPLAY_SHOP_NAME_VERSION,
                    ),
                )
            else:
                result = await api._settled_thread(lambda: _run(owner, operation, values, secret, device_id, actor))
            return _response(operation, result, version)
        except (ReceiptError, device_token.DeviceTokenError) as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_receipt_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_" + operation)

    async def redeem(request):
        try:
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if not auth_enabled() or "Cookie" in request.headers or "Authorization" in request.headers:
                return _device_error(400, "device_token_rejected")
            if not device_token.budget_http_enabled():
                raise api._Rejected(404, "budget_disabled")
            retry = device_token.redeem_limited(device_token.client_ip(request), time.monotonic())
            if retry is not None:
                return _device_error(429, "device_token_rejected", retry)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error or not isinstance(body, dict) or set(body) != {"code", "device_name"}:
                return _device_error(400, "device_token_rejected")
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if not device_token.budget_http_enabled():
                raise api._Rejected(404, "budget_disabled")
            code = device_token.normalize_code(body["code"])
            if code is None or device_token._device_name(body["device_name"]) is None:
                return _device_error(400, "device_token_rejected")
            retry = device_token.redeem_global_limited(time.monotonic())
            if retry is not None:
                return _device_error(429, "device_token_rejected", retry)
            result = await api._settled_thread(
                lambda: device_token.open_store().redeem(code, body["device_name"]))
            if result is None:
                return _device_error(400, "device_token_rejected")
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except BudgetUsersBackendRequired:
            return _device_error(400, "device_token_rejected")
        except Exception as exc:
            return api._failure(exc, "receipts_redeem")

    def require_login_flags():
        if not enabled():
            raise api._Rejected(404, "budget_receipts_disabled")
        if not device_token.budget_http_enabled():
            raise api._Rejected(404, "budget_disabled")

    async def login(request):
        try:
            require_login_flags()
            if "Cookie" in request.headers or "Authorization" in request.headers:
                return _device_error(400, "budget_auth_ambiguous")
            if "Content-Encoding" in request.headers:
                return _device_error(415, "budget_invalid_request")
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error:
                return body
            require_login_flags()
            form = api_contract.BudgetReceiptDeviceLoginRequest.model_validate(body)
            # JSON escape sequences can produce lone UTF-16 surrogates, which
            # cannot be passed to SQLite or the existing UTF-8 password hasher.
            form.username.encode("utf-8")
            form.password.encode("utf-8")
            name = device_token._device_name(form.device_name)
            if name is None:
                return _device_error(400, "budget_invalid_request")
            if not auth_enabled():
                return _device_error(401, "device_token_rejected")
            store = await api._settled_thread(device_token.open_store)
            try:
                ip = consent_context.login_ip(request)
            except (ValueError, BudgetConfigError):
                return _device_error(400, "budget_invalid_request")
            retry = login_limiter.take(form.username, ip, time.monotonic())
            if retry is not None:
                return _device_error(429, "device_token_rate_limited", retry)
            result = await api._settled_thread(
                lambda: store.login(form.username, form.password, name))
            if result is None:
                return _device_error(401, "device_token_rejected")
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except (ValidationError, UnicodeEncodeError):
            return _device_error(400, "budget_invalid_request")
        except BudgetUsersBackendRequired:
            return _device_error(401, "device_token_rejected")
        except device_token.DeviceTokenError as exc:
            return _device_error(exc.status, exc.code)
        except Exception as exc:
            return api._failure(exc, "receipts_device_login")

    async def browser_device(request):
        operation = request.path.rsplit("/", 1)[-1]
        device_id = request.get(device_token.REQUEST_KEY)
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if device_id is None:
                api._browser(request, owner, secret)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error:
                return body
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            code_id = None
            if device_id is not None:
                token_id = body.get("token_id") if isinstance(body, dict) and set(body) == {"token_id"} else None
            else:
                api._browser(request, owner, secret)
                values = _DEVICE_BROWSER_REQUESTS[operation].model_validate(body).model_dump()
                token_id = values.get("token_id")
                code_id = values.get("code_id")

            def run():
                store = device_token.open_store()
                if operation == "pair-code":
                    return store.issue_code(owner)
                if operation == "pair-code-cancel":
                    return store.cancel_code(owner, code_id)
                if operation == "list":
                    return store.list_tokens(owner)
                return store.revoke(
                    owner, token_id, actor_token_id=device_id,
                    actor_token_proof=request.get(device_token.PROOF_REQUEST_KEY),
                )

            result = await api._settled_thread(run)
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_device_" + operation)

    async def retention(_app):
        async def sweep():
            while True:
                try:
                    await api._settled_thread(retain_expired)
                except (Exception, asyncio.CancelledError):
                    if asyncio.current_task().cancelling():
                        raise
                await asyncio.sleep(3600)

        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        # The sweep task ends with CancelledError. Waiting through gather does not
        # catch that exception here, so a cancellation of this cleanup still propagates.
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(retention)
    for operation in READS:
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
    for operation in WRITES:
        app.router.add_post(PREFIX + "/" + operation, handle)
    for operation in _DEVICE_BROWSER_REQUESTS:
        app.router.add_post(PREFIX + "/device-token/" + operation, browser_device)
    app.router.add_post(device_token.REDEEM_PATH, redeem)
    app.router.add_post(device_token.LOGIN_PATH, login)


def _run_read(owner, operation, identifier, secret, device_id=None, actor=None, *,
              include_display_shop_name=False, include_evidence_target=False):
    store = _store(secret, device_id, actor)
    if operation == "detail":
        return store.detail(owner, identifier, include_display_shop_name=include_display_shop_name,
                            include_evidence_target=include_evidence_target)
    if operation == "group/detail":
        return store.group_detail(owner, identifier)
    store.image(owner, identifier)
    raise ReceiptError("budget_receipt_not_found", 404)
