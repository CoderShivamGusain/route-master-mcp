"""Booking-scam checks for links, phone numbers and messages.

Most bus-booking fraud in India is not a bad bus but a fake *booking*: lookalike
websites, fake helpline numbers in search ads, and WhatsApp "agents" who ask
for a UPI PIN, OTP or an advance to a personal UPI ID. Everything here is
offline pattern matching, so a clean result means "no red flags found", never
"guaranteed safe".

Used by the check_booking_safety tool, and by `booking/checkout.py` to confirm that a
checkout page is on an official host before payment.

`check` runs two passes over the text:
  1. Hosts: every URL, bare domain and email domain is compared with the
     verified domains in `operators.json`. Official hosts pass. "@" tricks,
     bare IPs, punycode and booking-platform lookalikes (brand name inside
     another domain, a one- or two-letter misspelling, digit-for-letter swaps)
     are HIGH risk. A government operator's name on an unverified domain,
     shorteners, WhatsApp/Telegram links and unknown sites are CAUTION.
  2. Message content: regexes for scam patterns (PIN/OTP/card requests,
     remote-access apps, QR "refunds", personal UPI IDs, advances, pressure,
     boarding-point changes, helpline numbers).
The verdict is the worst finding. Phone numbers can never be verified offline,
so they always add a caution.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from typing import Literal
from urllib.parse import urlsplit

from mcp_route_master.common.textutil import GENERIC_WORDS, edit_distance, load_data

Verdict = Literal["HIGH_RISK", "CAUTION", "LOOKS_OFFICIAL", "NO_RED_FLAGS_FOUND"]
Severity = Literal["high", "caution", "ok"]

MAX_INPUT_CHARS = 5000  # longer input is truncated; bounds regex work on pasted text
REPORT_LINE = "Report fraud at https://cybercrime.gov.in or call 1930 (national cyber-crime helpline)."

_COMMON_WORDS = {"kadamba", "swift"}  # also ordinary words/names in private sites
# Two-part public suffixes: the registrable domain of "x.redbus.co.in" is "redbus.co.in",
# not "co.in". A small hand list instead of the full Public Suffix List keeps this offline.
_MULTI_SUFFIX = {
    "co.in",
    "gov.in",
    "nic.in",
    "org.in",
    "net.in",
    "ac.in",
    "edu.in",
    "firm.in",
    "gen.in",
    "ind.in",
    "co.uk",
    "com.au",
}
_SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "cutt.ly",
    "is.gd",
    "rb.gy",
    "shorturl.at",
    "goo.gl",
    "t.ly",
    "ow.ly",
    "tiny.cc",
}
_WHATSAPP = {"wa.me", "whatsapp.com", "chat.whatsapp.com", "api.whatsapp.com", "t.me", "telegram.me"}
# Links that start with a scheme or "www.", up to whitespace, quotes or brackets.
_URL = re.compile(r"(?:\bhttps?://|\bwww\.)[^\s<>\"'()]+", re.I)
# A domain written without a scheme ("redbus-help.in"): DNS-style labels (max 63 chars, no
# leading/trailing hyphen) plus a 2-24 letter TLD. The lookbehind skips email domains and
# the tail of longer hostnames.
_BARE_HOST = re.compile(r"(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# "redbus.in@evil.com": a domain dressed up as the user part of an address. Limited to
# common TLDs so ordinary emails like "john.doe@gmail.com" aren't flagged.
_DOMAIN_LIKE = re.compile(
    r"(?:[a-z0-9-]+\.)+(?:in|com|org|net|co|info|online|site|app|io|me|biz|live|shop|top|xyz)", re.I
)
# A UPI ID is "handle@bank-suffix" with no dot after the "@"; the alternatives are the
# common bank/app suffixes (okaxis, ybl, paytm, ...).
_UPI_ID = re.compile(
    r"\b[\w.\-]{2,}@(?:ok\w+|ybl|ibl|axl|paytm|upi|apl|yapl|pty\w*|axisbank|icici|sbi|hdfcbank|"
    r"kotak|freecharge|airtel|jio|fbl|idfcbank|federal|indus|yesbank|barodampay|aubank)\b",
    re.I,
)
# Indian mobile numbers (10 digits starting 6-9, optional +91, "98765 43210" spacing)
# and 1800 toll-free numbers.
_PHONE = re.compile(r"(?:\+?91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b|\b1800[\s-]?\d{3}[\s-]?\d{3,4}\b")
# Digits used in place of look-alike letters ("redbu5", "make my tr1p"), undone before brand checks.
_HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "4": "a", "7": "t"})

# Verbs that turn a mention of an OTP into a request for one.
_SHARE = r"(share|send|tell|give|forward|enter|type|provide|confirm)"
# Patterns that are almost never legitimate in a booking conversation. `.{0,N}` windows
# require the two parts to be close together, to avoid matching unrelated sentences.
_HIGH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(upi|atm)\s*pin\b|\bm-?pin\b", re.I),
        "Mentions your UPI/ATM PIN. You never need a PIN to receive money or a refund; never share it.",
    ),
    (
        re.compile(
            rf"{_SHARE}\b.{{0,40}}\b(otp|one[\s-]*time[\s-]*password)\b|\b(otp|one[\s-]*time[\s-]*password)\b.{{0,40}}\b{_SHARE}",
            re.I,
        ),
        "Asks for an OTP. Never share an OTP with anyone, including 'customer care'.",
    ),
    (
        re.compile(r"\bcvv\b|\bcard\s*(number|no\.?|details)\b|\bexpiry\s*date\b", re.I),
        "Asks for card details (number/CVV/expiry). Only enter these on the official checkout page.",
    ),
    (
        re.compile(
            r"\b(any\s*desk|teamviewer|quick\s*support|rust\s*desk|airdroid)\b|screen[\s-]*shar", re.I
        ),
        "Asks you to install a remote-access app or share your screen. This is a common refund scam.",
    ),
    (
        re.compile(
            r"refund.{0,60}\b(scan|qr|approve|collect request|pin)\b|\b(scan|qr)\b.{0,60}refund", re.I
        ),
        "Refund that needs you to scan a QR code or approve a request. Scanning a QR or approving a request only ever sends money out of your account.",
    ),
    (
        re.compile(r"\.apk\b", re.I),
        "Asks you to install an app file (.apk) from outside the Play Store/App Store.",
    ),
]
# Patterns that are common in scams but can also appear in genuine messages.
_CAUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(whats\s*app|telegram)\b.{0,60}\b(book|booking|ticket|pay|payment|confirm|seat)|"
            r"\b(book|booking|ticket|pay|payment|confirm|seat)\b.{0,60}\b(whats\s*app|telegram)\b",
            re.I,
        ),
        "Booking or paying through WhatsApp/Telegram is not an official channel.",
    ),
    (
        re.compile(r"\b(advance|token amount|booking amount|pay first|deposit|registration fee)\b", re.I),
        "Asks for an advance/token payment before a ticket is issued.",
    ),
    (
        re.compile(
            r"\b(last seat|only \d+ seats?|hurry|urgent|today only|within \d+ (min|mins|minutes))\b", re.I
        ),
        "Uses pressure ('last seat', 'urgent'). Scammers rush you so you don't check.",
    ),
    (
        re.compile(r"\b(pick\s*up|pickup|boarding)\b.{0,40}\b(changed|shifted|moved|new location)\b", re.I),
        "Boarding point change sent by message. Confirm it in the booking site/app before going.",
    ),
    (
        re.compile(r"\b(customer\s*care|helpline|toll[\s-]*free|support number)\b", re.I),
        "Mentions a helpline. Fake helpline numbers in search ads are common; use the number in your "
        "ticket or the official app.",
    ),
]


@dataclass
class Finding:
    """One finding with a user-facing message."""

    severity: Severity
    message: str


@dataclass
class SafetyReport:
    """Result of `check`: the overall verdict, each finding, and which hosts were seen."""

    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    official_hosts: list[str] = field(default_factory=list)
    hosts_checked: list[str] = field(default_factory=list)
    phone_numbers: int = 0


@dataclass(frozen=True)
class _Candidate:
    """A host found in the text; `source` says whether it came from a URL, a bare domain or an email."""

    host: str
    raw: str
    source: Literal["url", "bare", "email"]
    userinfo: bool = False  # "redbus.in@evil.com": the browser ignores everything before "@"


@cache
def _official() -> tuple[dict[str, str], frozenset[str], dict[str, str]]:
    """(official domain -> owner, booking-platform brand words, government brand word -> owner)."""
    # Brand words are what lookalike detection searches for inside unknown hosts.
    data = load_data("operators.json")
    domains: dict[str, str] = {}
    platform_brands: set[str] = set()
    for platform in data["booking_platforms"]:
        for d in platform["domains"]:
            domains[d] = platform["name"]
            platform_brands.add(registrable(d).split(".")[0])
    gov_brands: dict[str, str] = {}
    for op in data["government_operators"]:
        for d in op.get("domains", []):
            domains[d] = op["short"]
            if d.endswith((".gov.in", ".nic.in")):
                # tgsrtc.telangana.gov.in -> "tgsrtc"; transport.rajasthan.gov.in -> nothing
                candidates = d.split(".")[:-2]
            else:
                candidates = [registrable(d).split(".")[0]]
            for c in candidates:
                if len(c) >= 4 and c not in GENERIC_WORDS and c not in platform_brands:
                    gov_brands.setdefault(c, op["short"])
        # Abbreviations scammers borrow even when we have no verified domain ("hrtc-online-booking.in").
        for name in op.get("names", []):
            if " " not in name and len(name) >= 4 and name not in GENERIC_WORDS and name not in _COMMON_WORDS:
                gov_brands.setdefault(name, op["short"])
    return domains, frozenset(platform_brands), gov_brands


def registrable(host: str) -> str:
    """The domain someone actually registered: 'book.redbus.in' -> 'redbus.in',
    'x.ksrtc.co.in' -> 'ksrtc.co.in'."""
    labels = host.lower().strip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIX:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _matches(host: str, domain: str) -> bool:
    """Host is the domain or a subdomain of it ('redbus.in.evil.com' is not)."""
    return host == domain or host.endswith("." + domain)


def _candidates(text: str) -> list[_Candidate]:
    """Find hosts in free text: full URLs first, then emails, then bare domains.

    Each pass blanks out what it matched so the next pass can't pick it up again.
    """
    found: list[_Candidate] = []
    for match in _URL.finditer(text):
        raw = match.group(0).rstrip(".,;:!?")
        # Browsers treat "\" like "/", so "evil.com\@redbus.in" really opens evil.com.
        url = raw.replace("\\", "/")
        url = url if url.lower().startswith("http") else "http://" + url
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            # The address as a person reads it (split on "/" only, so a "\\@" trick stays visible).
            as_read = re.split(r"[/?#]", re.sub(r"^[a-z]+://", "", raw, flags=re.I), maxsplit=1)[0]
        except ValueError:
            continue
        if host:
            disguised = "@" in parts.netloc or "@" in as_read
            found.append(_Candidate(host, raw, "url", userinfo=disguised))
    rest = _URL.sub(" ", text)
    for match in _EMAIL.finditer(rest):
        local, domain = match.group(0).rsplit("@", 1)
        disguised = bool(_DOMAIN_LIKE.fullmatch(local))
        found.append(_Candidate(domain.lower(), match.group(0), "url" if disguised else "email", disguised))
    rest = _EMAIL.sub(" ", rest)
    for match in _BARE_HOST.finditer(rest):
        host = match.group(1).lower()
        if not host.replace(".", "").isdigit():
            found.append(_Candidate(host, host, "bare"))
    unique: dict[tuple[str, bool], _Candidate] = {}
    for c in found:
        unique.setdefault((c.host, c.userinfo), c)
    return list(unique.values())


def extract_hosts(text: str) -> list[tuple[str, str]]:
    """Return (host, original text) pairs for links and email domains found in free text."""
    return [(c.host, c.raw) for c in _candidates(unicodedata.normalize("NFKC", text))]


def _is_ip(host: str) -> bool:
    if host.isdigit() or host.startswith("0x"):
        return True  # "http://2130706433/" is 127.0.0.1 written as a number
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _check_host(c: _Candidate, report: SafetyReport) -> None:
    """Add findings for one host. Checks run from most to least specific and mostly stop at
    the first hit, so each host normally gets a single finding."""
    domains, platform_brands, gov_brands = _official()
    host = c.host.removeprefix("www.")
    report.hosts_checked.append(host)
    add = report.findings.append

    if c.userinfo:
        add(
            Finding(
                "high",
                f"{c.raw}: disguised link. Browsers ignore everything before '@', so this "
                f"really opens {host}.",
            )
        )
        return
    if _is_ip(host):
        add(Finding("high", f"{c.raw}: link goes to a bare IP address, not a website name."))
        return
    if "xn--" in host:
        add(Finding("high", f"{host}: uses look-alike (punycode) characters."))
        return

    owner = next((name for d, name in domains.items() if _matches(host, d)), None)
    if owner:
        if c.source == "email":
            return
        report.official_hosts.append(f"{host} ({owner})")
        if c.raw.lower().startswith("http://"):
            add(Finding("caution", f"{c.raw}: official site but not https; type the address yourself."))
        if c.raw.lower().split("?")[0].endswith(".apk"):
            add(
                Finding(
                    "high", f"{c.raw}: app file download; install apps only from the Play Store/App Store."
                )
            )
        return

    reg = registrable(host)
    if reg in _SHORTENERS:
        add(Finding("caution", f"{host}: shortened link hides where it really goes."))
        return
    if reg in _WHATSAPP or host in _WHATSAPP:
        add(Finding("caution", f"{host}: WhatsApp/Telegram link, not a booking website."))
        return

    # Undo visual tricks before looking for brand names: digits for letters, "rn" for "m",
    # "vv" for "w". `compact` also drops separators so "red-bus" still contains "redbus".
    flat = host.translate(_HOMOGLYPHS).replace("rn", "m").replace("vv", "w")
    compact = flat.replace("-", "").replace(".", "")
    tokens = set(re.split(r"[.\-]", flat))
    label = reg.split(".")[0].translate(_HOMOGLYPHS)
    # Booking-platform brands: any use outside the official domain is HIGH risk.
    for brand in sorted(platform_brands):
        if brand in compact:
            add(
                Finding("high", f"{host}: uses the name '{brand}' but is NOT the official site. Likely fake.")
            )
            return
        # Misspellings ("redbos", "makemytirp"): allow 1 edit for brands up to 7 letters, 2 for
        # longer ones. Brands under 6 letters are skipped: too many real words are 1 edit away.
        limit = 1 if len(brand) <= 7 else 2
        if len(brand) >= 6 and edit_distance(label, brand, limit) <= limit:
            add(Finding("high", f"{host}: looks like '{brand}' with a spelling change. Likely fake."))
            return
    # Government brands are only CAUTION: some RTCs book through domains we haven't verified.
    # Short brands must be a whole label ("hrtc-booking.in"), longer ones may be embedded.
    for brand, owner_name in sorted(gov_brands.items()):
        if brand in tokens or (len(brand) >= 6 and brand in compact):
            add(
                Finding(
                    "caution",
                    f"{host}: uses the name of {owner_name} but isn't one of its verified "
                    "domains. Only use it if you reached it from the operator's .gov.in site.",
                )
            )
            return

    if host.endswith((".gov.in", ".nic.in")):
        if c.source != "email":
            add(
                Finding(
                    "caution",
                    f"{host}: a government domain, but not one on record for bus booking. "
                    "Reach booking pages from the transport department's own site.",
                )
            )
        return
    if c.source != "email":
        add(Finding("caution", f"{host}: not a known bus-booking site. Check it carefully before paying."))


def check(text: str) -> SafetyReport:
    """Scan a link, phone number or pasted message and return a `SafetyReport`."""
    # NFKC turns full-width look-alikes ("ｒｅｄｂｕｓ") into the ASCII a browser would use.
    text = unicodedata.normalize("NFKC", text or "")[:MAX_INPUT_CHARS]
    report = SafetyReport(verdict="NO_RED_FLAGS_FOUND")

    for candidate in _candidates(text):
        _check_host(candidate, report)

    for pattern, message in _HIGH_PATTERNS:
        if pattern.search(text):
            report.findings.append(Finding("high", message))
    if _UPI_ID.search(_EMAIL.sub(" ", text)):  # emails have a dotted domain; UPI IDs don't
        report.findings.append(
            Finding(
                "high",
                "Asks you to pay a personal UPI ID. Official sites take payment on their own "
                "checkout page, never to a person's UPI ID.",
            )
        )
    for pattern, message in _CAUTION_PATTERNS:
        if pattern.search(text):
            report.findings.append(Finding("caution", message))

    report.phone_numbers = len(_PHONE.findall(text))
    if report.phone_numbers:
        report.findings.append(
            Finding(
                "caution",
                "Phone numbers can't be verified offline. Use only the number printed on your "
                "ticket or shown inside the official app/site.",
            )
        )

    severities = {f.severity for f in report.findings}
    if "high" in severities:
        report.verdict = "HIGH_RISK"
    elif "caution" in severities:
        report.verdict = "CAUTION"
    elif report.official_hosts:
        report.verdict = "LOOKS_OFFICIAL"
    return report
