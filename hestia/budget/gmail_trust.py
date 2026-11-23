"""Authenticate Gmail envelopes and select the original content of spouse forwards."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import getaddresses
import re

from .gmail_address import canonical_domain, canonical_mailbox
from .gmail_invoice import has_provider_invoice_number

_PUBLIC_WEBMAIL = frozenset({
    "gmail.com", "icloud.com", "wp.pl", "onet.pl", "o2.pl", "interia.pl", "outlook.com", "yahoo.com",
})
_CONFIG_ERROR = "budget_receipt_gmail_sender_config_invalid"

_QUOTED_VALUE = r'"(?:[^"\\]|\\.)*"'
_AUTH_TOKEN = re.compile(rf'{_QUOTED_VALUE}|\\.|[();]|[^"\\();]+|["\\]', re.DOTALL)
_AUTH_RESULT = re.compile(r"\s*(dkim|spf|dmarc)(?:/[0-9]+)?\s*=\s*pass(?=\s|$)", re.ASCII | re.IGNORECASE)
_AUTH_PROPERTY = re.compile(rf'\s+([a-z][a-z0-9._-]*)\s*=\s*({_QUOTED_VALUE}|[^\s"]+)', re.ASCII | re.IGNORECASE)
_MAX_AUTH_CHARS = 16 * 1024

_MAX_FORWARD_DEPTH = 2
_MAX_FORWARD_BYTES = 1024 * 1024
_MAX_FORWARD_HEADER_LINES = 32
_MAX_FORWARD_HEADER_CHARS = 4096
_RFC822_TRANSFER_ENCODINGS = frozenset({"7bit", "8bit", "binary"})
_FORWARD_MARKERS = frozenset({"forwarded message", "begin forwarded message", "przekazana wiadomość"})
_FORWARD_HEADERS = frozenset({
    "from", "od", "date", "data", "sent", "wysłano", "subject", "temat", "to", "do", "cc", "dw",
    "reply-to", "message-id",
})


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


def normalize_sender_rules(sender_allowlist, spouse_forward_senders, known_merchant_domains):
    """Return normalized direct rules, exact spouse mailboxes and merchant domains."""
    direct = _normalize_rules(sender_allowlist, _sender_rule)
    spouse = _normalize_rules(spouse_forward_senders, canonical_mailbox)
    merchants = set(_normalize_rules(known_merchant_domains, _domain_rule))
    if not direct and not spouse:
        raise ValueError(_CONFIG_ERROR)
    # An exact public-webmail rule remains exact even inside a forward.
    merchants.update(rule.rpartition("@")[2] for rule in direct)
    return direct, spouse, tuple(sorted(merchants - _PUBLIC_WEBMAIL))


def _mailbox(value):
    addresses = getaddresses([value], strict=True)
    if len(addresses) == 1:
        return canonical_mailbox(addresses[0][1])
    return None


def _domain_matches(domain, rule):
    return domain == rule or domain.endswith(f".{rule}")


def _on_allowlist(address, rules):
    domain = address.rpartition("@")[2]
    return any(address == rule or (rule.startswith("@") and _domain_matches(domain, rule[1:]))
               for rule in rules)


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


def classify_sender(headers, sender_allowlist, spouse_forward_senders, owner_mailbox=None):
    """Classify ordered outer headers, preserving duplicates and the topmost AR."""
    by_name = {}
    for name, value in headers:
        by_name.setdefault(name.lower(), []).append(value)
    authors = by_name.get("from", ())
    address = _mailbox(authors[0]) if len(authors) == 1 else None
    if not address:
        return "untrusted"
    domain = address.rpartition("@")[2]
    sender_headers = by_name.get("sender", ())
    if sender_headers:
        sender = _mailbox(sender_headers[0]) if len(sender_headers) == 1 else None
        if not sender or sender.rpartition("@")[2] != domain:
            return "untrusted"
    if not _gmail_authenticated(domain, by_name.get("authentication-results", ()),
                               by_name.get("dkim-signature", ()),
                               relaxed=_on_allowlist(address, sender_allowlist)):
        return "untrusted"
    if owner_mailbox and address == owner_mailbox:
        return "owner_forward"
    if address in spouse_forward_senders:
        return "spouse_forward"
    if _on_allowlist(address, sender_allowlist):
        return "direct_allowlist"
    return "untrusted"


def _inline_forward(text, sender_allowlist, *, require_receipt=True):
    lines = text.splitlines()
    markers = [i for i, line in enumerate(lines) if line.strip(" \t-:").casefold() in _FORWARD_MARKERS]
    if len(markers) != 1:
        return None
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
            return None
        if name == "od":
            name = "from"
        if name in fields:
            return None
        fields[name] = value.strip()
    else:
        return None
    address = _mailbox(fields.get("from", ""))
    if not address:
        return None
    body = "\n".join(lines[index:])
    if require_receipt and (not _on_allowlist(address, sender_allowlist)
                            or not has_provider_invoice_number(body)):
        return None
    original = EmailMessage()
    original["From"] = address
    if fields.get("message-id"):
        original["Message-ID"] = fields["message-id"]
    original.set_content(body)
    return original


def _inline_body(message, render_html):
    body = message.get_body(("plain", "html") if render_html else ("plain",))
    if body is None:
        return None
    payload = body.get_payload(decode=True)
    if not payload or len(payload) > _MAX_FORWARD_BYTES:
        return None
    try:
        text = payload.decode(body.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return None
    if body.get_content_subtype() == "html":
        utf8 = text.encode("utf-8")
        return render_html(utf8) if len(utf8) <= _MAX_FORWARD_BYTES else None
    return text


def mime_parts(message):
    """Walk the current MIME envelope without descending into forwarded messages."""
    parts = [message]
    while parts:
        part = parts.pop()
        yield part
        if part.get_content_maintype() == "multipart":
            parts.extend(reversed(list(part.iter_parts())))


def _forwarded_original(message, sender_allowlist, render_html, *, require_receipt=True):
    wrappers = [part for part in mime_parts(message) if part.get_content_type() == "message/rfc822"]
    if wrappers:
        if len(wrappers) != 1:
            return None
        wrapper = wrappers[0]
        encodings = [value.strip().lower() for name, value in wrapper.raw_items()
                     if name.lower() == "content-transfer-encoding"]
        # RFC 2046 §5.2.1 applies these encodings to the wrapper. The
        # embedded message can still transfer-encode its own body parts.
        if len(encodings) > 1 or (encodings and encodings[0] not in _RFC822_TRANSFER_ENCODINGS):
            return None
        originals = list(wrapper.iter_parts())
        if len(originals) != 1 or originals[0].defects:
            return None
        return originals[0]
    text = _inline_body(message, render_html)
    return _inline_forward(text, sender_allowlist, require_receipt=require_receipt) if text else None


def unwrap_forward(message, known_merchant_domains, require_known=True, *,
                   sender_allowlist=(), render_html=None):
    """Select one original, after the caller has classified the outer spouse sender.

    Every relay must be a configured merchant. The legacy require_known option
    cannot bypass this boundary. Inline HTML uses the supplied isolated worker.
    """
    for _ in range(_MAX_FORWARD_DEPTH):
        message = _forwarded_original(message, sender_allowlist, render_html)
        if message is None:
            return None
        authors = [value for name, value in message.raw_items() if name.lower() == "from"]
        address = _mailbox(authors[0]) if len(authors) == 1 else None
        if not address:
            return None
        domain = address.rpartition("@")[2]
        if not (_on_allowlist(address, sender_allowlist)
                or any(_domain_matches(domain, rule) for rule in known_merchant_domains)):
            return None
        if not any(part.get_content_type() == "message/rfc822" for part in mime_parts(message)):
            return message
    return None


def _hop_authorized(message, sender_allowlist, known_merchant_domains):
    authors = [value for name, value in message.raw_items() if name.lower() == "from"]
    address = _mailbox(authors[0]) if len(authors) == 1 else None
    if not address:
        return False
    domain = address.rpartition("@")[2]
    return (_on_allowlist(address, sender_allowlist)
            or any(_domain_matches(domain, rule) for rule in known_merchant_domains))


def forward_sender_domain(message):
    """Return the inner From domain. Inner Authentication-Results are ignored."""
    authors = [value for name, value in message.raw_items() if name.lower() == "from"]
    address = _mailbox(authors[0]) if len(authors) == 1 else None
    if not address:
        return None
    return address.rpartition("@")[2]


def unwrap_trusted_forward(message, known_merchant_domains, *, sender_allowlist=(), render_html=None):
    """Unwrap one owner or spouse forward, at most two levels.

    Returns ``(message, manual)``. ``manual`` is true when a hop is outside the
    allowlist and known merchant domains. A third forward, a malformed part, or
    a message that is not a forward returns None. Inner authentication headers
    are not consulted.
    """
    current = message
    hops = []
    for _depth in range(_MAX_FORWARD_DEPTH):
        nxt = _forwarded_original(current, sender_allowlist, render_html, require_receipt=False)
        if nxt is None:
            break
        hops.append(nxt)
        current = nxt
    if not hops:
        return None
    if len(hops) == _MAX_FORWARD_DEPTH and _forwarded_original(
            current, sender_allowlist, render_html, require_receipt=False) is not None:
        return None
    manual = any(not _hop_authorized(hop, sender_allowlist, known_merchant_domains) for hop in hops)
    return hops[-1], manual
