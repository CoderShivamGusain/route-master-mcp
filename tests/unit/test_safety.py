import pytest

from mcp_route_master.trust.safety import check, extract_hosts, registrable


@pytest.mark.parametrize(
    "text",
    [
        "https://www.redbus.in/bus-tickets/delhi-to-manali",
        "makemytrip.com",
        "Book on https://bus.irctc.co.in/ today",
        "https://www.abhibus.com/buses/108/Delhi-Manali",
        "https://tgsrtcbus.in",
        "https://rsrtconline.rajasthan.gov.in",
    ],
)
def test_official_sites(text):
    assert check(text).verdict == "LOOKS_OFFICIAL"


@pytest.mark.parametrize(
    "url",
    [
        "http://redbus-booking.in/pay",
        "https://redbus.in.secure-pay.com/",
        "https://rebdus.in",
        "https://redbuss.in",
        "https://www.makemytripp.com",
        "https://irctc-bus-booking.co",
        "https://abhibus-offers.online",
        "https://g0ibibo.com",
        "http://103.21.4.9/book",
        "https://xn--rdbus-2ra.in",
    ],
)
def test_lookalike_and_suspicious_links(url):
    assert check(url).verdict == "HIGH_RISK", url


def test_unknown_site_is_caution_not_official():
    r = check("https://cheapbustickets.example.com")
    assert r.verdict == "CAUTION" and not r.official_hosts


def test_generic_words_do_not_trigger_brand_match():
    # 'transport' and 'ebooking' come from official gov domains but are generic words.
    assert check("https://citytransport-news.com").verdict == "CAUTION"


@pytest.mark.parametrize(
    "message",
    [
        "Please share the OTP you received to confirm your seat",
        "For refund enter your UPI PIN",
        "Install AnyDesk so our executive can process your refund",
        "Scan this QR code to receive your refund",
        "Pay 500 to sharma.travels@okaxis to confirm",
        "Send card number and CVV",
        "Download our app: http://bookbus.example.com/app.apk",
    ],
)
def test_scam_messages(message):
    assert check(message).verdict == "HIGH_RISK", message


def test_caution_messages():
    r = check("Booking on WhatsApp, pay advance and seat confirmed, hurry last seat")
    assert r.verdict == "CAUTION"
    assert len(r.findings) >= 3


def test_phone_number_is_never_called_safe():
    r = check("Call customer care 9876543210")
    assert r.verdict == "CAUTION" and r.phone_numbers == 1


def test_plain_text_has_no_red_flags():
    assert check("Bus leaves at 9 pm from the main stand").verdict == "NO_RED_FLAGS_FOUND"


def test_email_is_not_upi_and_not_a_host():
    r = check("Write to support@redbus.in")
    assert r.verdict in {"NO_RED_FLAGS_FOUND"}
    assert extract_hosts("mail me at john.doe@gmail.com") == [("gmail.com", "john.doe@gmail.com")]
    assert check("mail me at john.doe@gmail.com").verdict == "NO_RED_FLAGS_FOUND"


def test_registrable():
    assert registrable("bus.irctc.co.in") == "irctc.co.in"
    assert registrable("www.redbus.in") == "redbus.in"


def test_long_input_is_truncated_not_rejected():
    assert check("hello " * 5000).verdict == "NO_RED_FLAGS_FOUND"


# ---- adversarial cases from code review -------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "redbus.in@evil.com",
        "https://redbus.in@evil.com/x",
        "https://evil.com\\@redbus.in",
        "Mail support@redbus-refund.com for refund",
        "https://www.makemytrip.com.secure-login.net/pay",
        "https://irctc.co.in.refund-help.com",
        "HTTPS://REDBUS-OFFERS.IN",
        "https://redbus.in:443@evil.com",
        "http://2130706433/",
        "http://[::1]/book",
        "https://ｒｅｄｂｕｓ-pay.in",
    ],
)
def test_disguised_links_are_high_risk(text):
    assert check(text).verdict == "HIGH_RISK", text


def test_full_width_official_domain_is_read_as_ascii():
    assert check("https://ｒｅｄｂｕｓ.in").verdict == "LOOKS_OFFICIAL"


@pytest.mark.parametrize(
    "url",
    ["https://fastcash.in", "https://podcastcentral.com", "https://eastcoast.in", "https://redbull.com"],
)
def test_short_brand_words_do_not_cause_false_alarms(url):
    assert check(url).verdict == "CAUTION"  # unknown site, not "likely fake"


@pytest.mark.parametrize(
    "url", ["https://apsrtconline.in", "https://upsrtconline.co.in", "https://gsrtc.in", "https://osrtc.org"]
)
def test_unverified_rtc_sites_are_caution_not_fake(url):
    r = check(url)
    assert r.verdict == "CAUTION", url
    assert not any(f.severity == "high" for f in r.findings)


def test_unknown_gov_subdomain_is_not_called_official():
    assert check("https://refund.evil.nic.in").verdict == "CAUTION"


def test_normal_email_is_fine():
    assert check("Ticket sent to me at priya@gmail.com").verdict == "NO_RED_FLAGS_FOUND"


def test_rtc_name_on_unverified_site_is_called_out():
    r = check("https://hrtc-online-booking.in/pay")
    assert r.verdict == "CAUTION"
    assert any("HRTC" in f.message for f in r.findings)
