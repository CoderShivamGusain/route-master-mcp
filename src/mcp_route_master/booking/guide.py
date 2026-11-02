"""Step-by-step script for booking a chosen bus in the user's own browser.

The server cannot book (no API, no scraping). A browser-capable AI host follows
these steps on the official site; every step that touches personal data or money
is a hard stop that needs the user.

Used by the booking_steps tool. Each `Step` is an instruction for the host;
`stop=True` marks a point where the host must wait for the user (choosing
seats or boarding points, permission to enter personal details, the final
"confirm"). The host never pays, enters OTPs or logs in: it stops on the
payment page and hands over the link. Site-specific scripts exist for redBus
and MakeMyTrip because their element labels and page flow were verified;
other sites get a shorter generic script. The host follows this text
literally, so edit the wording with care.

Checked live:
- redBus (1 and 2 Nov 2026, guest checkout, no login): seat map (step=SL) -> boarding/dropping
  (step=BPDP) -> passenger info (step=CI) -> /paymentDetails (tax added there, seat held ~8 min).
  In a narrow window redBus shows its mobile layout: tap the bus card instead of 'View seats'.
- MakeMyTrip (2 Nov 2026, guest, no login): results page -> 'Select Seats' opens an in-page seat
  map with a pickup/drop panel -> CONTINUE opens /bus/review (traveller form) -> CONTINUE -> payment.
  The last CONTINUE ignored automated clicks (bot checks), so the user presses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Site = Literal["redBus", "MakeMyTrip", "other"]

_COMMON_INPUTS = [
    "Each passenger's full name (as on photo ID), age and gender, and which seat they sit in",
]

# What the user must decide or provide before the AI may fill the form, per site.
USER_INPUTS: dict[str, list[str]] = {
    "redBus": _COMMON_INPUTS
    + [
        "Mobile number for the ticket (10 digits) and, optionally, email",
        "State of residence (redBus asks it for the GST invoice; it may pre-fill one, so confirm it)",
        "WhatsApp trip updates: on or off",
        "Paid add-ons: free cancellation, trip guarantee, travel insurance - yes or no for each",
    ],
    "MakeMyTrip": _COMMON_INPUTS
    + [
        "Mobile number for the ticket (10 digits) and email (MakeMyTrip requires both)",
        "Paid add-ons: Free Cancellation (priced per person) and trip insurance - yes or no for each",
    ],
    "other": _COMMON_INPUTS
    + [
        "Mobile number for the ticket (10 digits) and email",
        "Any paid add-ons the site offers - yes or no for each",
    ],
}


@dataclass(frozen=True)
class Step:
    """One instruction for the AI host."""

    text: str
    stop: bool = False  # True = wait for the user before continuing


def redbus_steps(operator: str, departure: str, seats: list[str]) -> list[Step]:
    """redBus guest checkout: seats -> boarding/dropping -> passenger info -> payment page."""
    seat_list = ", ".join(seats)
    return [
        Step(
            f"Open the dated redBus results link. Find '{operator}' departing {departure} and click its "
            f"'View seats' button (aria-label 'View seats for {operator}'), or tap the bus card in the mobile "
            "layout. Close the optional login pop-up if it appears; do not log in."
        ),
        Step(
            "Read every seat from its aria-label ('Seat number N, lower deck, seat type ..., berth ..., price N "
            "rupees, availability ...') plus its on-screen x/y, and call explain_seat_map. Show the diagram."
        ),
        Step(
            f"Confirm the seats with the user (planned: {seat_list}). Never pick seats the user didn't name.",
            stop=True,
        ),
        Step(
            "Click each chosen seat (the button whose aria-label starts 'Seat number <N>,'). Re-read the labels "
            "to confirm each shows as selected; if a seat was taken meanwhile, stop and tell the user."
        ),
        Step("Click 'Select boarding & dropping points'. The URL gets step=BPDP."),
        Step(
            "Read the 'Boarding points' and 'Dropping points' radio options (time + place) and ask the user to "
            "choose. Warn if a point is a petrol pump, toll or bypass (check_operator can flag it).",
            stop=True,
        ),
        Step(
            "Click the chosen boarding and dropping radios and check both are selected. The page moves to "
            "Passenger Info (step=CI); in the mobile layout press 'Fill passenger details'."
        ),
        Step(
            "Ask permission to enter the passenger and contact details, and collect anything missing "
            "(see 'Details to collect'). Do not continue without a clear yes.",
            stop=True,
        ),
        Step(
            "Fill Contact details: Phone (country code +91), Email if given, State of Residence (redBus may "
            "pre-fill it: confirm with the user); set the WhatsApp switch (on or off by default) to the "
            "user's choice. Do not tap 'Login to view saved passengers'."
        ),
        Step(
            "Fill each 'Passenger N · Seat X' block: Name, Age, Gender (redBus offers Male/Female). Match each "
            "passenger to the seat the user said."
        ),
        Step(
            "For each add-on (Free Cancellation, Trip Guarantee, Travel Insurance) click the option the user "
            "chose; default to the 'Don't add' option. Note the prices of any added."
        ),
        Step(
            "Read the page: selected seats, boarding/dropping, passengers, contact, add-ons and the total. "
            "Call booking_summary with the page URL, these values, requested_seats and add_ons. Use the "
            "boarding point's time as departure and the page's amount (before tax) as total_fare."
        ),
        Step(
            "Show the summary. If it lists problems, fix them on the page and call booking_summary again. "
            "Continue only when it lists no problems and the user replies 'confirm' (or a clear yes) to it. "
            "Before clicking, check the page still shows the same details; if anything changed, run "
            "booking_summary again, show it and wait for a new 'confirm'.",
            stop=True,
        ),
        Step(
            "Click 'Continue booking' once and stop on the payment page (/paymentDetails). Hand over in one "
            "short message: the total shown (tax included; if it's more than the approved fare plus ~5% tax, "
            "say what was added), the time left on the page's timer, and 'Pay here (redbus.in): <page link>'. The user pays (UPI/card) themselves; OTP or login is theirs too. If the click does "
            "nothing, don't retry: give the passenger-page link and tell them to press 'Continue booking'.",
            stop=True,
        ),
        Step(
            "After the user pays, redBus sends the ticket by SMS/email. Offer to check any message they receive "
            "with check_booking_safety."
        ),
    ]


def makemytrip_steps(operator: str, departure: str, seats: list[str]) -> list[Step]:
    """MakeMyTrip guest checkout. Its seat map has no numbers until a seat is selected, and the
    final CONTINUE may ignore automated clicks, so those steps hand control to the user."""
    seat_list = ", ".join(seats)
    return [
        Step(
            f"Open the dated MakeMyTrip results link. Find '{operator}' departing {departure} (check operator, "
            "bus type and time on the same card) and click that card's 'SELECT SEATS'. Don't log in."
        ),
        Step(
            "Read the seat map: lower and upper berth panels, each seat's price, and whether it's free "
            "(white/outlined), sold (grey) or men/women-only (blue/pink person icon). MakeMyTrip shows no seat "
            "numbers yet: label by position (L1, L2... lower; U1... upper, front to back, left to right) and "
            "pass x/y. Its data-testid can say 'available' for greyed berths; trust the grey look (and a "
            "'Disabled' icon class). Call explain_seat_map with site='MakeMyTrip' and labels_are_positions=true; show the diagram."
        ),
        Step(
            f"Confirm the seats with the user (planned: {seat_list}). Never pick seats the user didn't name.",
            stop=True,
        ),
        Step(
            "Click each chosen seat, then read 'Selected Seats' on the right: it now shows the site's real seat "
            "number (e.g. 'B') and price. If a different seat got selected, click it again to clear it and retry. "
            "Tell the user the real number; use it from here on (also as requested_seats)."
        ),
        Step(
            "Read the 'PICK UP POINT' and 'DROP POINT' lists in the same panel (time + place). Ask the user to "
            "choose; the departure time depends on the pickup point. Warn about petrol pumps, tolls or bypasses.",
            stop=True,
        ),
        Step(
            "Click the chosen pickup and drop entries, check both show as selected, then click CONTINUE. The "
            "traveller page opens at /bus/review with selectedSeats, bpId and dpId in the URL; check it shows "
            "the right seat, pickup, drop and times."
        ),
        Step(
            "Ask permission to enter the passenger and contact details, and collect anything missing "
            "(see 'Details to collect'). Do not continue without a clear yes.",
            stop=True,
        ),
        Step(
            "Fill each 'Seat X' block: Name, Age, Gender (a men-only or women-only seat locks the other gender). "
            "Fill Email Id and Mobile Number. Leave GST details empty unless the user asks."
        ),
        Step(
            "Free Cancellation: click the user's choice ('Add Free Cancellation at ₹N/person' or 'No I don't "
            "need it'). Insurance ('Secure your trip at just ₹N'): tick only if the user said yes. Don't apply "
            "coupons that need a login or a specific card."
        ),
        Step(
            "Read the page: seat, pickup/drop and times, passengers, contact, add-ons and 'Amount'. Call "
            "booking_summary with the page URL, these values, requested_seats (the site's numbers) and add_ons. "
            "Use the pickup time as departure."
        ),
        Step(
            "Show the summary. If it lists problems, fix them on the page and call booking_summary again. "
            "Continue only when it lists no problems and the user replies 'confirm' (or a clear yes) to it. "
            "Before clicking, check the page still shows the same details; if anything changed, run "
            "booking_summary again, show it and wait for a new 'confirm'.",
            stop=True,
        ),
        Step(
            "Click CONTINUE once (it accepts MakeMyTrip's terms) and stop on the payment page. Hand over in "
            "one short message: the total (flag anything above fare plus tax), the time left, and 'Pay here "
            "(makemytrip.com): <page link>'. If nothing happens, "
            "don't retry or work around it (MakeMyTrip can ignore automated clicks): give the /bus/review "
            "link and tell the user to press CONTINUE; in your browser pane the form stays filled, elsewhere "
            "they retype the details. The user pays themselves.",
            stop=True,
        ),
        Step(
            "After the user pays, MakeMyTrip sends the ticket by SMS/email. Offer to check any message they "
            "receive with check_booking_safety."
        ),
    ]


def generic_steps(site: str, operator: str, departure: str, seats: list[str]) -> list[Step]:
    """Site-neutral script for platforms without a verified flow."""
    seat_list = ", ".join(seats)
    return [
        Step(
            f"Open the dated {site} results. Find '{operator}' at {departure} and open its seat selection "
            "(e.g. 'Select Seats'). Don't log in."
        ),
        Step(
            "Read every seat (number or position, deck, price, sold/available, women/men-only) and call "
            "explain_seat_map. If the site shows no numbers, label seats by position, pass labels_are_positions=true and say so."
        ),
        Step(f"Confirm the seats with the user (planned: {seat_list}).", stop=True),
        Step(
            "Select the seats and check the site shows exactly those as selected (note its seat numbers), then "
            "the boarding and dropping points the user chooses.",
            stop=True,
        ),
        Step(
            "Ask permission, then fill contact and passenger details on the site's form. Never enter OTPs, "
            "passwords, UPI PIN or card details.",
            stop=True,
        ),
        Step("Leave paid add-ons off unless the user asked for them."),
        Step(
            "Call booking_summary with what the page shows; continue only when it lists no problems and the "
            "user replies 'confirm' to it (a changed page needs a new summary and a new 'confirm').",
            stop=True,
        ),
        Step(
            "Open the payment page and stop. Hand over: the total (flag anything above fare plus tax), the time left, and 'Pay here "
            "(<site>): <page link>'. "
            "If the site ignores the click, give the user the page link to press it.",
            stop=True,
        ),
    ]


def build(site: Site, operator: str, departure: str, seats: list[str]) -> list[Step]:
    """Steps for booking `seats` on the given bus at `site`."""
    if site == "redBus":
        return redbus_steps(operator, departure, seats)
    if site == "MakeMyTrip":
        return makemytrip_steps(operator, departure, seats)
    return generic_steps("the booking site", operator, departure, seats)


def inputs_for(site: Site) -> list[str]:
    """Details the user must provide or decide before the host fills the site's forms."""
    return USER_INPUTS.get(site, USER_INPUTS["other"])


def traveller_steps(site: Site, operator: str, departure: str) -> list[str]:
    """Steps for the traveller to follow themselves, for AI apps that can't control the user's
    browser (ChatGPT, Gemini). Plain language, no page code; the safety checks are the same."""
    name = "the booking site" if site == "other" else site
    seat_tip = (
        "The seat map shows no numbers until you tap a seat; the number appears under 'Selected Seats'."
        if site == "MakeMyTrip"
        else "Each seat shows its number and price; grey ones are sold."
    )
    extras = {
        "redBus": "Free Cancellation, Trip Guarantee and Travel Insurance",
        "MakeMyTrip": "Free Cancellation (priced per person) and trip insurance",
    }.get(site, "any paid extras")
    return [
        f"Open {name} from the link above (type the address yourself if you prefer). You don't need to log in.",
        f"Find **{operator}** leaving at **{departure}** and open its seats. {seat_tip}",
        "Pick your seat or berth. On sleeper buses, berths cost more than the 'from' price in the list.",
        "Choose where you'll board and get off. The departure time depends on the boarding point; avoid "
        "highway petrol pumps or toll plazas if there's a bus stand or office option.",
        "Fill in each passenger's name as on their photo ID, age and gender, plus a mobile number and email "
        "you can receive the ticket on.",
        f"Decide on {extras}: leave them off unless you want them.",
        "Before paying, check: the address bar shows the real site with https; the seats, names, boarding "
        "point and total are right. If you like, paste the page text here and I'll check it.",
        "Pay on the site's own payment page. Never share an OTP, UPI PIN or card details with anyone, and "
        "never pay to a personal UPI ID or over WhatsApp.",
        "The ticket arrives by SMS/email. Paste any message about the booking here if it looks odd.",
    ]
