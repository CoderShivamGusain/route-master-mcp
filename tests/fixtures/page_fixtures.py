"""Page captures recorded live on 2 Nov 2026 (redBus mobile layout and MakeMyTrip desktop),
in the shape `page_scripts.CAPTURE_JS` returns. Personal details are the user's test data.

Travel dates in the URLs are moved to a week from today, so checks such as "this bus already
left" don't make the tests depend on the time of day they run.
"""

import re
from datetime import timedelta

from mcp_route_master.places.dates import redbus_date, today_ist

TRAVEL = today_ist() + timedelta(days=7)
_RB = redbus_date(TRAVEL)  # 09-Nov-2026
_MMT = TRAVEL.strftime("%d-%m-%Y")  # 09-11-2026
_MMT_UTC = (TRAVEL - timedelta(days=1)).isoformat() + "T18%3A30%3A00.000Z"  # midnight IST in UTC

REDBUS_RESULTS = {
    "v": 1,
    "url": "https://www.redbus.in/bus-tickets/delhi-to-manali?fromCityId=733&fromCityName=Delhi&toCityId=757"
    f"&toCityName=Manali&onward={_RB}&doj={_RB}",
    "title": "Delhi to Manali Bus – Book from 210 Buses",
    "text": "Delhi Manali\n104 buses\n03 Nov\nTue\nDelhi to Manali Bus\nFilter & Sort\nUse Code RTCNEW to Get Upto"
    " ₹300 off on HRTC!\nHRTC Buses\nहिमाचल रोड ट्रान्सपोर्ट कॉर्पोरेशन\n9 buses\nstarting from\n₹895\n20:00\n08:00",
    "cards": [
        "20:00 | 08:00 | 12h | 36 Seats | ₹1,600 | Onwards | Himachal Pradesh Tourism Development Corporation"
        " (HPTDC) - 49531 | Volvo A/C Seater (2+2) | 3.7 | 29",
        "22:25 | 09:55 | 11h 30m | 41 Seats | ₹797 | Onwards | ReoBus Pro | Volvo 9600 Multi Axle Semi-Sleeper"
        " (2+2) | 4.8 | 658 | Comfort score: 9.1/10 | On Time",
        "Try new | 10% OFF | 22:15 | 09:15 | 11h | 27 Seats | ₹709 | ₹638 | Onwards | Bedi Travels | Volvo 9600"
        " Multi Axle Semi-Sleeper (2+2) | 4.8 | 656 | Comfort score: 9.3/10 | Free date change | New Bus",
        "23:00 | 10:00 | 11h | 34 Seats | ₹689 | Onwards | Deltin Travels Pvt Ltd | Ad | Volvo 9600 A/C Semi"
        " Sleeper (2+2) | 4.3 | 296 | Comfort score: 8.7/10 | Free date change",
        "17:00 | 11:30 | 18h 30m | 24 Seats | (6 Single) | ₹2,199 | Onwards | Tiwari and Brothers | A/C"
        " Seater/Sleeper (2+1) | 4.5 | 72 | Toilet",
    ],
}

MMT_RESULTS = {
    "v": 1,
    "url": f"https://www.makemytrip.com/bus/search/Dehradun/Agra/{_MMT}?from_code=MMTCC1198&to_code=MMTCC3641",
    "text": "23 buses found\nSORT BY\nRelevance\nFastest\nCheapest\nRating\nArrival\nDeparture\nUPSRTC\nUttar Pradesh"
    " State Road Transport Corporation\n2 Buses\n₹ 670\nVIEW BUSES\nUTC\nUttarakhand Transport Corporation\n2 Buses\n"
    "₹ 630\nVIEW BUSES\nRTH STAR BUS\nBharat Benz A/C Seater /Sleeper (2+1)\n21:452 NOV\n06hrs 45mins\n04:303 NOV\n"
    "949\n4.7\n59 Ratings\n43 Seats Left\nPolicies\nPhotos\nAmenities\nPickups & Drops\nReviews\nSELECT SEATS\n"
    "Shri Krishna Travels ( Jai Shree Ganesh Yatra Co.)\nVE A/C Seater / Sleeper (2+1)\n20:302 NOV\n11hrs 30mins\n"
    "08:003 NOV\n799\n4.1\n136 Ratings\nLive Tracking\n12 Seats Left\n6 Window Seats\nPolicies\nPhotos\nAmenities\n"
    "Pickups & Drops\nReviews\nSELECT SEATS\nPTC-SkyBus\nBharat Benz A/C Seater /Sleeper (2+1)\n19:002 NOV\n09hrs"
    " 00mins\n04:003 NOV\n1,300\n4.1\n94 Ratings\nLive Tracking\n25 Seats Left\n12 Window Seats\nPolicies\nPhotos\n"
    "Amenities\nPickups & Drops\nReviews\nSELECT SEATS\nManeesha Tour And Travels\nNON A/C Seater (2+3)\n19:002 NOV"
    "\n09hrs 00mins\n04:003 NOV\n600\n2.4\n55 Seats Left\n19 Window Seats\nPolicies\nPhotos\nAmenities\nPickups &"
    " Drops\nReviews\nSELECT SEATS",
}

# The site shows each time with its day ("21:452 NOV"); recorded on 2 Nov, moved to TRAVEL like the URL.
_DAYS = {"2": TRAVEL, "3": TRAVEL + timedelta(days=1)}
MMT_RESULTS["text"] = re.sub(
    r"(\d{2}:\d{2})([23]) NOV",
    lambda m: f"{m[1]}{_DAYS[m[2]].day} {_DAYS[m[2]].strftime('%b').upper()}",
    MMT_RESULTS["text"],
)

_SEAT = "Seat number {n}, {deck} deck, seat type {kind}, berth {berth}, price {price} rupees, availability {a}, currently {cur}"


def _rb(n, deck, kind, berth, price, a, x, y, cur="unselected"):
    return {"l": _SEAT.format(n=n, deck=deck, kind=kind, berth=berth, price=price, a=a, cur=cur), "p": [x, y]}


REDBUS_SEATS = {
    "v": 1,
    "url": "https://www.redbus.in/bus-tickets/dehradun-to-varanasi?fromCityId=777&fromCityName=Dehradun&toCityId=70429"
    f"&toCityName=Varanasi&onward={_RB}&doj={_RB}&ref=search&step=SL",
    "text": "Select seats",
    "seats": [
        _rb("2", "lower", "seater", "reserved for male", 2199, "sold", 259, 51),
        _rb("1", "lower", "seater", "reserved for male", 2199, "available", 219, 51),
        _rb("4L", "lower", "sleeper", "unreserved", 3999, "available", 139, 50),
        _rb("4", "lower", "seater", "unreserved", 2199, "available", 259, 95),
        _rb("3", "lower", "seater", "unreserved", 2199, "available", 219, 95),
        _rb("8L", "lower", "sleeper", "reserved for male", 3999, "sold", 139, 138),
        _rb("3U", "upper", "sleeper", "unreserved", 3499, "available", 343, -38, cur="selected"),
        _rb("2U", "upper", "sleeper", "reserved for male", 3299, "sold", 423, -38),
        _rb("1U", "upper", "sleeper", "reserved for male", 3299, "sold", 463, -38),
        _rb("14U", "upper", "sleeper", "reserved for female", 3299, "available", 423, 226),
        _rb("13U", "upper", "sleeper", "reserved for male", 3299, "sold", 463, 226),
        _rb("15U", "upper", "sleeper", "reserved for male", 3499, "sold", 343, 226),
    ],
}

REDBUS_POINTS = {
    "v": 1,
    "url": REDBUS_SEATS["url"].replace("step=SL", "step=BPDP"),
    "text": "Boarding points\nDropping points",
    "radios": [
        {
            "n": "bp_0",
            "on": False,
            "t": "17:00 | Garhwal Parking ( Bramhan wala Chowk haridwar by pass road) | Garhwal"
            " Parking ( Bramhan wala Chowk haridwar by pass road) Dehradun",
        },
        {
            "n": "bp_1",
            "on": False,
            "t": "17:25 | Mall Of Dehradun | Mall Of Dehradun (Dehradun-Haridwar road, IIP Mokhampur)",
        },
        {"n": "bp_2", "on": True, "t": "17:40 | Doiwala Bypass Dehradun"},
        {"n": "dp_0", "on": False, "t": "11:30 | 03 Nov | Mudaila (Sardar Petrol Pump)"},
    ],
}

REDBUS_FORM = {
    "v": 1,
    "url": REDBUS_SEATS["url"].replace("step=SL", "step=CI"),
    "text": "Passenger Info\nBus on time or 50% cashback\nTiwari and Brothers\n02 Nov · 17:40\nDoiwala Bypass Dehradun\n"
    "03 Nov · 11:30\nMudaila (Sardar Petrol Pump)\n1 seat\nView details\nContact details\nTicket details will be sent"
    " to\nCountry Code\n+91 (IND)\nPhone *\nEmail ID\nState of Residence *\nRequired for GST Tax Invoicing\nSend"
    " booking details and trip updates on WhatsApp\nPassenger details\nLogin to view saved passengers list\nPassenger"
    " 1\nSeat 3U, Upper Deck\nName *\nAge *\nGender *\n(required)\nGender (required)\nMale\n, 1 of 2\n, Selected\n"
    "Female\n, 2 of 2\n, Not selected\nTrip Guarantee\n₹65 per passenger\nAmount\n(Tax excluded)\n₹3,499\nContinue"
    " booking",
    "inputs": [
        {"id": "0_6", "type": "tel", "label": "", "v": "9876543210"},
        {"id": "0_5", "type": "email", "label": "Enter email id", "v": "abc@gmail.com"},
        {"id": "0_201", "type": "text", "label": "", "v": "Delhi"},
        {"id": "whatsapp_toggle", "type": "checkbox", "label": "", "v": False},
        {"id": "0_4", "type": "text", "label": "Enter your Name", "v": "Ram Singh"},
        {"id": "0_1", "type": "number", "label": "Enter Age", "v": "31"},
    ],
    "radios": [
        {"n": "tg", "on": False, "t": "Add Trip Guarantee | ₹65 for 1 passenger"},
        {"n": "tg", "on": True, "t": "Don’t add Trip Guarantee"},
        {"n": "ins", "on": False, "t": "Add Travel Insurance | ₹15 for 1 passenger"},
        {"n": "ins", "on": True, "t": "Don’t add Travel Insurance"},
    ],
}

REDBUS_FORM_SAVED_CONTACT = {
    **REDBUS_FORM,
    "text": REDBUS_FORM["text"].replace(
        "Ticket details will be sent to\nCountry Code\n+91 (IND)\nPhone *\nEmail ID\nState of Residence *\nRequired for"
        " GST Tax Invoicing\nSend booking details and trip updates on WhatsApp",
        "Ticket details will be sent to\nEdit\n+91 9876543210\nabc@gmail.com\nDelhi\nWhatsApp communication enabled",
    ),
    "inputs": REDBUS_FORM["inputs"][4:],
}

REDBUS_PAYMENT = {
    "v": 1,
    "url": "https://www.redbus.in/paymentDetails",
    "text": "Pay ₹3,673.95\n07:48\nReview booking\n1 Seat · Tiwari and Brothers\nView details\nDehradun\nVaranasi\n"
    "Mon, 02 Nov · 17:40\nSecure Payment",
}


def _mmt(t, c, price, x, y, i):
    return {"t": t, "c": c, "price": price, "p": [x, y], "i": i}


MMT_SEATS = {
    "v": 1,
    "url": f"https://www.makemytrip.com/bus/search/Dehradun/Agra/{_MMT}?from_code=MMTCC1198&to_code=MMTCC3641",
    "text": "Select Seats\nKnow your seat\nLOWER BERTH (4)\nUPPER BERTH (4)\nSelect Pickup & Drop Points\nPICK UP POINT\n"
    "20:30, 2 NOV\nBrahman Wala Chowk\nShri Krishna Tour And Travels,Opposite - Sanskriti Lok Colony,BrahamanWala"
    " Chowk, haridwar By pass Dehradun\n20:45, 2 NOV\nIsbt\nNear Kailash Hospital, joggi wala, Dehradun\n20:50, 2 NOV\n"
    "Isbt\nin front of mall of dehradun gate\nDROP POINT\n07:30, 3 NOV\nIsbt bus stand\nisbt bus stand near transport"
    " nagar\n08:00, 3 NOV\nAgra\nRadhika Travels,Near Rakab Ganj Police Thana ,Chipitola ,Agra -282001\nSelected"
    " Seats\nB\n₹1,099\nFare Details\nCONTINUE",
    "mmt_seats": [
        _mmt("seat_horizontal_sleeper_available", "commonBigSeatIcon", "₹1299", 64, 39, 0),
        _mmt("seat_seater_available", "commonSmallSeatIcon", "₹799", 152, 39, 1),
        _mmt("seat_seater_unavailable", "commonSmallSeatDisabledIcon", "₹799", 152, 84, 2),
        # testid says available but the icon is greyed (sold): the icon wins
        _mmt("seat_vertical_sleeper_available", "commonBigSeatDisabledIcon", "₹1199", 196, 563, 3),
        _mmt("seat_horizontal_sleeper_available", "maleBigSeatSelectedIcon", "₹1099", 402, 39, 4),
        _mmt("seat_horizontal_sleeper_unavailable", "commonBigSeatDisabledIcon", "₹1099", 362, 39, 5),
        _mmt("seat_horizontal_sleeper_available", "femaleBigSeatIcon", "₹1099", 402, 354, 6),
        _mmt("seat_horizontal_sleeper_available", "commonBigSeatIcon", "₹1099", 362, 462, 7),
    ],
    "selected": [
        "20:50, 2 NOV | Isbt | in front of mall of dehradun gate",
        "08:00, 3 NOV | Agra | Radhika Travels,Near Rakab Ganj Police Thana ,Chipitola ,Agra -282001",
    ],
}

MMT_FORM = {
    "v": 1,
    "url": f"https://www.makemytrip.com/bus/review?from=Dehradun&to=Agra&date={_MMT_UTC}"
    "&bpId=27556866&dpId=19508867&selectedSeats=B&isSponsored=false",
    "text": "Complete your booking\nDehradun to Agra\nShri Krishna Travels ( Jai Shree Ganesh Yatra Co.)\nSeat No: B\nVE"
    " A/C Seater / Sleeper (2+1)\nView Policies\n20:50 2 Nov' 26, Mon\nDehradun\nMall of dehradun\nin front of mall of"
    " dehradun gate\n11h 10m\n08:00 3 Nov' 26, Tue\nAgra\nAgra\nRadhika Travels,Near Rakab Ganj Police Thana"
    " ,Chipitola ,Agra -282001\nTraveller Details\nSeat B\n(Male only)\nName\nAge*\nGender\nMale\nFemale\nContact"
    " Details We’ll send your ticket here\nPrice details\nBase Fare\n₹1099.0\nAmount\n₹1099\nFinal payable amount will"
    " be updated on the next page\nCONTINUE",
    "inputs": [
        {"id": "fname", "type": "text", "label": "Type here", "v": "Ram Singh"},
        {"id": "age", "type": "number", "label": "eg : 24", "v": "31"},
        {"id": "contactEmail", "type": "email", "label": "", "v": "abc@gmail.com"},
        {"id": "mobileNumber", "type": "text", "label": "Type here", "v": "9876543210"},
        {"id": "toggleSwitch", "type": "checkbox", "label": "Enter GST details (optional)", "v": False},
        {"id": "", "type": "checkbox", "label": "Secure your trip at just ₹15 Powered by", "v": False},
    ],
    "choices": [
        {"t": "Add Free Cancellation at ₹143/person Free Cancellation till 02 Nov, 02:30 PM", "on": False},
        {"t": "No I don't need it", "on": True},
    ],
    "active": ["Male"],
}
