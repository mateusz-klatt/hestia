"""Authenticate Gmail envelopes and select the original content of spouse forwards."""
from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import getaddresses
import re
from typing import Literal, get_args

from .gmail_address import canonical_domain, canonical_mailbox

_PUBLIC_WEBMAIL = frozenset({
    "gmail.com", "icloud.com", "wp.pl", "onet.pl", "o2.pl", "interia.pl", "outlook.com", "yahoo.com",
})
_CONFIG_ERROR = "budget_receipt_gmail_sender_config_invalid"
_MAX_PAYMENT_SENDERS = 200
PaymentProvider = Literal["p24", "tpay", "autopay", "payu", "paynow", "stripe", "blik"]
_PAYMENT_PROVIDER_DOMAINS: dict[str, PaymentProvider] = {
    "przelewy24.pl": "p24",
    "tpay.com": "tpay",
    "autopay.pl": "autopay",
    "payu.com": "payu",
    "paynow.pl": "paynow",
    "stripe.com": "stripe",
}

_QUOTED_VALUE = r'"(?:[^"\\]|\\.)*"'
_AUTH_TOKEN = re.compile(rf'{_QUOTED_VALUE}|\\.|[();]|[^"\\();]+|["\\]', re.DOTALL)
_AUTH_RESULT = re.compile(r"\s*(dkim|spf|dmarc)(?:/[0-9]+)?\s*=\s*pass(?=\s|$)", re.ASCII | re.IGNORECASE)
_AUTH_PROPERTY = re.compile(rf'\s+([a-z][a-z0-9._-]*)\s*=\s*({_QUOTED_VALUE}|[^\s"]+)', re.ASCII | re.IGNORECASE)
_MAX_AUTH_CHARS = 16 * 1024

_INVALID_FORWARD = object()
_MAX_FORWARD_DEPTH = 2
_MAX_FORWARD_BYTES = 1024 * 1024
_MAX_FORWARD_HEADER_LINES = 32
_MAX_FORWARD_HEADER_CHARS = 4096
_RFC822_TRANSFER_ENCODINGS = frozenset({"7bit", "8bit", "binary"})
_FORWARD_MARKERS = frozenset({
    "forwarded message",
    "begin forwarded message",
    "przekazana wiadomość",
    "wiadomość przekazana dalej",
})
_FORWARD_HEADERS = frozenset({
    "from", "od", "date", "data", "sent", "wysłano", "subject", "temat", "to", "do", "cc", "dw",
    "reply-to", "message-id",
})


@dataclass(frozen=True)
class PaymentSender:
    sender: str
    merchant: str
    gateway: bool = False
    provider: PaymentProvider | None = None

    def __post_init__(self):
        if self.gateway and self.provider is None:
            domain = self.sender.rpartition("@")[2]
            for registered_domain, provider in _PAYMENT_PROVIDER_DOMAINS.items():
                if domain_matches(domain, registered_domain):
                    object.__setattr__(self, "provider", provider)
                    break


def _domain_rule(value):
    domain = canonical_domain(value.strip(" \t").removeprefix("@"))
    if domain in _PUBLIC_WEBMAIL:
        return None
    return domain


def _sender_rule(value):
    if value.strip(" \t").startswith("@"):
        domain = _domain_rule(value)
        return f"@{domain}" if domain else None
    return canonical_mailbox(value)


def _normalize_rules(values, normalize):
    if not isinstance(values, (list, tuple)):
        raise ValueError(_CONFIG_ERROR)
    rules = set()
    for value in values:
        normalized = normalize(value) if isinstance(value, str) else None
        if not normalized:
            raise ValueError(_CONFIG_ERROR)
        rules.add(normalized)
    return tuple(sorted(rules))


def _payment_sender_rule(entry) -> PaymentSender:
    if (not isinstance(entry, dict) or not {"sender", "merchant"} <= entry.keys()
            or entry.keys() - {"sender", "merchant", "gateway", "provider"}):
        raise ValueError(_CONFIG_ERROR)
    sender, merchant = entry["sender"], entry["merchant"]
    gateway = entry.get("gateway", False)
    rule = _sender_rule(sender) if isinstance(sender, str) else None
    if rule is None or any(domain_matches(rule.rpartition("@")[2], domain) for domain in _PUBLIC_WEBMAIL):
        raise ValueError(_CONFIG_ERROR)
    if (not isinstance(merchant, str) or not 1 <= len(merchant) <= 64
            or not merchant.isprintable() or not merchant.strip()):
        raise ValueError(_CONFIG_ERROR)
    if type(gateway) is not bool:
        raise ValueError(_CONFIG_ERROR)
    provider = entry.get("provider")
    if "provider" in entry and (not gateway or provider not in get_args(PaymentProvider)):
        raise ValueError(_CONFIG_ERROR)
    return PaymentSender(rule, merchant, gateway, provider)


def normalize_payment_senders(values) -> tuple[PaymentSender, ...]:
    if not isinstance(values, list) or len(values) > _MAX_PAYMENT_SENDERS:
        raise ValueError(_CONFIG_ERROR)
    rules = {}
    for value in values:
        entry = _payment_sender_rule(value)
        if entry.sender in rules:
            raise ValueError(_CONFIG_ERROR)
        rules[entry.sender] = entry
    return tuple(rules.values())


def normalize_document_senders(values):
    """Validate document senders using the payment sender grammar without gateway options."""
    if not isinstance(values, list) or any(
        not isinstance(entry, dict) or set(entry) != {"sender", "merchant"} for entry in values
    ):
        raise ValueError(_CONFIG_ERROR)
    return normalize_payment_senders(values)


def normalize_sender_rules(sender_allowlist, spouse_forward_senders, known_merchant_domains, *, payment_senders=(),
                           document_senders=()):
    """Return normalized direct rules, exact spouse mailboxes and merchant domains."""
    direct = _normalize_rules(sender_allowlist, _sender_rule)
    spouse = _normalize_rules(spouse_forward_senders, canonical_mailbox)
    merchants = set(_normalize_rules(known_merchant_domains, _domain_rule))
    if not direct and not spouse and not payment_senders and not document_senders:
        raise ValueError(_CONFIG_ERROR)
    # Match in both directions to catch either a parent domain or an exact mailbox
    # covered by the other list's domain rule.
    if any(_matches_sender(payment.sender, rule) or _matches_sender(rule, payment.sender)
           for payment in payment_senders for rule in direct):
        raise ValueError(_CONFIG_ERROR)
    merchants.update(rule[1:] for rule in direct if rule.startswith("@"))
    return direct, spouse, tuple(sorted(merchants))


def _mailbox(value):
    addresses = getaddresses([value], strict=True)
    if len(addresses) == 1:
        return canonical_mailbox(addresses[0][1])
    return None


def _single_mailbox(values):
    return _mailbox(values[0]) if len(values) == 1 else None


def domain_matches(domain: str, rule: str) -> bool:
    """Match a canonical domain to a rule for that domain and its subdomains."""
    return domain == rule or domain.endswith(f".{rule}")


def _matches_sender(address, rule):
    return address == rule or (rule.startswith("@") and domain_matches(address.rpartition("@")[2], rule[1:]))


def payment_sender_for(headers, payment_senders: tuple[PaymentSender, ...]) -> PaymentSender | None:
    """Resolve an authenticated From by exact mailbox, then by most specific domain."""
    address = _single_mailbox([value for name, value in headers if name.lower() == "from"])
    if address is None:
        return None
    matches = (entry for entry in payment_senders if _matches_sender(address, entry.sender))
    return max(matches, key=lambda entry: (entry.sender == address, len(entry.sender)), default=None)


def _split_auth_results(value):
    """Split an Authentication-Results field outside comments and quoted values."""
    if len(value) > _MAX_AUTH_CHARS:
        return []
    clauses = [[]]
    depth = 0
    for token in _AUTH_TOKEN.findall(value):
        if token == "(":
            if depth == 0:
                clauses[-1].append(" ")
            depth += 1
        elif token == ")":
            if depth == 0:
                return []
            depth -= 1
        elif depth == 0:
            if token == ";":
                clauses.append([])
            elif token == '"' or token.startswith("\\"):
                return []
            else:
                clauses[-1].append(token)
    return [] if depth else ["".join(clause) for clause in clauses]


def _auth_properties(value):
    properties = {}
    value = value.rstrip()
    offset = 0
    while offset < len(value):
        match = _AUTH_PROPERTY.match(value, offset)
        if not match:
            return None
        name, prop = match.groups()
        name = name.lower()
        if name in properties:
            return None
        if prop.startswith('"'):
            prop = re.sub(r"\\(.)", r"\1", prop[1:-1])
        properties[name] = prop
        offset = match.end()
    return properties


def _dkim_tags(signature):
    tags = {}
    for field in signature.split(";"):
        if not field.strip():
            continue
        name, sep, value = field.partition("=")
        name = name.strip()
        if not sep or not name or name in tags:
            return {}
        tags[name] = value.strip()
    return tags


def _signing_domain(properties, signatures):
    if "header.d" in properties:
        return canonical_domain(properties["header.d"])
    evidence = {name: properties[f"header.{name}"] for name in ("i", "b", "s")
                if f"header.{name}" in properties}
    if not evidence or not all(evidence.values()):
        return None

    matches = []
    for signature in signatures:
        tags = _dkim_tags(signature)
        effective_identity = tags.get("i", "@" + tags.get("d", ""))
        signature_b = "".join(tags.get("b", "").split())
        if "i" in evidence and evidence["i"] != effective_identity:
            continue
        if "b" in evidence and not signature_b.startswith(evidence["b"]):
            continue
        if "s" in evidence and evidence["s"] != tags.get("s"):
            continue
        matches.append(tags.get("d", ""))
    if len(matches) == 1:
        return canonical_domain(matches[0])
    return None


def _pass_domain(method, properties, signatures):
    if method == "dkim":
        return _signing_domain(properties, signatures)
    if method == "dmarc":
        return canonical_domain(properties.get("header.from", ""))
    value = properties.get("smtp.mailfrom", "")
    address = canonical_mailbox(value)
    return address.rpartition("@")[2] if address else canonical_domain(value)


def _domains_align(authenticated, domain, *, relaxed):
    """Strict equality, or a From subdomain of a multi-label authenticated domain."""
    if not authenticated or authenticated == domain:
        return bool(authenticated) and authenticated == domain
    return (relaxed and len(authenticated.split(".")) >= 2
            and domain.endswith("." + authenticated))


def _gmail_authenticated(domain, results, signatures, *, relaxed=False):
    if not results:
        return False
    # Gmail strips incoming Authentication-Results claiming its own authserv-id.
    clauses = _split_auth_results(results[0])
    if not clauses or clauses[0].strip().lower() != "mx.google.com":
        return False
    aligned = set()
    for clause in clauses[1:]:
        result = _AUTH_RESULT.match(clause)
        if not result:
            continue
        properties = _auth_properties(clause[result.end():])
        method = result[1].lower()
        # DMARC header.from stays an exact From-domain match. DKIM and SPF may use
        # the allowlist's subdomain rule; everyone else stays strict.
        if properties is not None and _domains_align(
            _pass_domain(method, properties, signatures), domain,
            relaxed=relaxed and method != "dmarc",
        ):
            aligned.add(method)
    return "dkim" in aligned and bool(aligned & {"spf", "dmarc"})


def classify_sender(headers, sender_allowlist, spouse_forward_senders, owner_mailbox=None, *, payment_senders=(),
                    document_senders=()):
    """Classify ordered outer headers, preserving duplicates and the topmost AR."""
    by_name = {}
    for name, value in headers:
        by_name.setdefault(name.lower(), []).append(value)
    address = _single_mailbox(by_name.get("from", ()))
    if not address:
        return "untrusted"
    domain = address.rpartition("@")[2]
    sender_headers = by_name.get("sender", ())
    if sender_headers:
        sender = _single_mailbox(sender_headers)
        if not sender or sender.rpartition("@")[2] != domain:
            return "untrusted"
    invoice_sender = any(_matches_sender(address, rule) for rule in sender_allowlist)
    if address == owner_mailbox:
        path = "owner_forward"
    elif address in spouse_forward_senders:
        path = "spouse_forward"
    elif invoice_sender or any(_matches_sender(address, entry.sender) for entry in (*payment_senders, *document_senders)):
        path = "direct_allowlist"
    else:
        return "untrusted"
    if not _gmail_authenticated(domain, by_name.get("authentication-results", ()),
                               by_name.get("dkim-signature", ()),
                               relaxed=invoice_sender or path == "direct_allowlist"):
        return "untrusted"
    return path


def _is_forward_marker(line):
    return line.strip(" \t-:").casefold() in _FORWARD_MARKERS


def _split_inline_forward(lines):
    markers = [i for i, line in enumerate(lines) if _is_forward_marker(line)]
    if not markers:
        return None
    if len(markers) != 1:
        return _INVALID_FORWARD
    marker = markers[0]
    fields = {}
    chars = 0
    end = min(len(lines), marker + 1 + _MAX_FORWARD_HEADER_LINES)
    for index in range(marker + 1, end):
        line = lines[index]
        if not line.strip() and not fields:
            continue
        name, sep, value = line.partition(":")
        name = name.strip().casefold()
        if not sep or name not in _FORWARD_HEADERS:
            break
        chars += len(line) + 1
        if chars > _MAX_FORWARD_HEADER_CHARS:
            return _INVALID_FORWARD
        if name == "od":
            name = "from"
        elif name == "data":
            name = "date"
        elif name == "temat":
            name = "subject"
        if name in fields:
            return _INVALID_FORWARD
        fields[name] = value.strip()
    else:
        return _INVALID_FORWARD
    return fields, index


def _inline_forward(text):
    lines = text.splitlines()
    split = _split_inline_forward(lines)
    if split is None or split is _INVALID_FORWARD:
        return split
    fields, index = split
    address = _mailbox(fields.get("from", ""))
    if not address:
        return _INVALID_FORWARD
    original = EmailMessage()
    original["From"] = address
    if fields.get("subject"):
        original["Subject"] = fields["subject"]
    if fields.get("date"):
        original["Date"] = fields["date"]
    if fields.get("message-id"):
        original["Message-ID"] = fields["message-id"]
    original.set_content("\n".join(lines[index:]))
    return original


def inline_forward_body(text: str) -> str:
    """Return the body after a single inline forward marker and headers, or text if absent."""
    lines = text.splitlines()
    split = _split_inline_forward(lines)
    if isinstance(split, tuple):
        return "\n".join(lines[split[1]:])
    return "" if split is _INVALID_FORWARD else text


def _inline_original(message, render_html):
    original = None
    for subtype in ("plain", "html"):
        part = message.get_body((subtype,))
        if part is None or (subtype == "html" and render_html is None):
            continue
        payload = part.get_payload(decode=True)
        if payload is None or part.defects or len(payload) > _MAX_FORWARD_BYTES:
            return _INVALID_FORWARD
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            return _INVALID_FORWARD
        if subtype == "html":
            if not text.strip():
                continue
            utf8 = text.encode("utf-8")
            if len(utf8) > _MAX_FORWARD_BYTES:
                return _INVALID_FORWARD
            text = render_html(utf8)
        candidate = _inline_forward(text)
        if candidate is _INVALID_FORWARD:
            return _INVALID_FORWARD
        if candidate is None:
            continue
        if original is None:
            original = candidate
        elif any(original.get(name) != candidate.get(name) for name in ("From", "Message-ID")):
            return _INVALID_FORWARD
    return original


def mime_parts(message):
    """Walk the current MIME envelope without descending into forwarded messages."""
    parts = [message]
    while parts:
        part = parts.pop()
        yield part
        if part.get_content_maintype() == "multipart":
            parts.extend(reversed(list(part.iter_parts())))


def _forwarded_original(message, render_html):
    """Return the next envelope, None at the end, or _INVALID_FORWARD."""
    wrappers = [part for part in mime_parts(message) if part.get_content_type() == "message/rfc822"]
    inline = _inline_original(message, render_html)
    if not wrappers:
        return inline
    if len(wrappers) != 1 or inline is not None:
        return _INVALID_FORWARD

    wrapper = wrappers[0]
    encodings = [value.strip().lower() for name, value in wrapper.raw_items()
                 if name.lower() == "content-transfer-encoding"]
    # RFC 2046 §5.2.1 applies these encodings to the wrapper. The
    # embedded message can still transfer-encode its own body parts.
    if wrapper.defects or len(encodings) > 1 or (encodings and encodings[0] not in _RFC822_TRANSFER_ENCODINGS):
        return _INVALID_FORWARD
    originals = list(wrapper.iter_parts())
    if len(originals) != 1 or originals[0].defects:
        return _INVALID_FORWARD
    return originals[0]


def _forward_sender(message):
    return _single_mailbox([value for name, value in message.raw_items() if name.lower() == "from"])


def forward_sender_domain(message):
    """Return the inner From domain."""
    address = _forward_sender(message)
    return address.rpartition("@")[2] if address else None


def unwrap_trusted_forward(message, *, sender_allowlist=(), render_html=None):
    """Return the final original and whether it needs manual review."""
    current = message
    manual = False
    for _ in range(_MAX_FORWARD_DEPTH):
        original = _forwarded_original(current, render_html)
        if original is None:
            break
        if original is _INVALID_FORWARD:
            return None
        address = _forward_sender(original)
        if address is None:
            return None
        manual |= address not in sender_allowlist
        current = original
    else:
        if _forwarded_original(current, render_html) is not None:
            return None
    return (current, manual) if current is not message else None
