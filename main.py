"""
Fake Expedia (Rapid API v3) test supplier for credit-hold QA.

Built to match what Trip-stick/expedia-shopping-worker and expedia-booking-worker
(dev-k8s) send and parse. Register it as an Expedia credential whose base_url
points here; no worker code changes.

Every property has a scenario that decides how search, price check and booking behave.
Room names start with the scenario ("Will Pending - Double Room") so search shows it.
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from collections import deque
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

API_KEY = os.getenv("API_KEY", "")
SHARED_SECRET = os.getenv("SHARED_SECRET", "")
STRICT_AUTH = os.getenv("STRICT_AUTH", "false").lower() == "true"
TIMEOUT_SLEEP = float(os.getenv("TIMEOUT_SLEEP_SECONDS", "60"))   # > credential "timeout"
SLOW_SLEEP = float(os.getenv("SLOW_SLEEP_SECONDS", "90"))         # case 21 window
PRICE_CHANGE_PCT = float(os.getenv("PRICE_CHANGE_PERCENT", "10"))
TAX_PCT = float(os.getenv("TAX_PERCENT", "15"))
DEFAULT_SCENARIO = os.getenv("DEFAULT_SCENARIO", "CONFIRM")       # for property ids not listed below
NRF_CANCEL = os.getenv("NRF_CANCEL", "refuse")                     # refuse | allow
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")                         # set when hosted publicly

SCENARIOS = {
    "CONFIRM": "Will Confirm",
    "PENDING": "Will Pending",
    "ERROR": "Will Fail",
    "PRICE_CHANGED": "Will Change Price",
    "PRICE_MISMATCH": "Will Fail - Price Mismatch",
    "SOLD_OUT": "Will Sell Out",
    "TIMEOUT_NOT_BOOKED": "Will Timeout - Not Booked",
    "TIMEOUT_BOOKED_REFUNDABLE": "Will Timeout - Booked Refundable",
    "TIMEOUT_BOOKED_NRF": "Will Timeout - Booked Non-Refundable",
    "SLOW_CONFIRM": "Will Confirm Slowly",
}

DEFAULT_PROPERTIES = {
    # property_id: (scenario, base price per room per night in the search currency)
    "99000001": ("CONFIRM", 100),
    "99000002": ("CONFIRM", 150),
    "99000003": ("PENDING", 120),
    "99000004": ("ERROR", 90),
    "99000005": ("PRICE_CHANGED", 110),
    "99000006": ("PRICE_MISMATCH", 110),
    "99000007": ("SOLD_OUT", 95),
    "99000008": ("TIMEOUT_NOT_BOOKED", 95),
    "99000009": ("TIMEOUT_BOOKED_REFUNDABLE", 105),
    "99000010": ("TIMEOUT_BOOKED_NRF", 85),
    "99000011": ("SLOW_CONFIRM", 100),
}

PROPERTIES = {}
ITINERARIES = {}
LOG = deque(maxlen=500)


def reset_state():
    PROPERTIES.clear()
    PROPERTIES.update({pid: {"scenario": s, "price": p} for pid, (s, p) in DEFAULT_PROPERTIES.items()})
    ITINERARIES.clear()
    LOG.clear()


reset_state()


def prop(pid):
    return PROPERTIES.get(str(pid)) or {"scenario": DEFAULT_SCENARIO, "price": 100}


def label(pid):
    s = prop(pid)["scenario"]
    name = SCENARIOS[s]
    if s == "SLOW_CONFIRM":
        name += f" ({int(SLOW_SLEEP)}s)"
    return name


def is_nrf(pid):
    return prop(pid)["scenario"] == "TIMEOUT_BOOKED_NRF"


# ---------- tokens (stateless: everything needed is inside the token) ----------

def enc(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def dec(t):
    t = t.split("&")[0]
    return json.loads(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))


def err(status, type_, message):
    return JSONResponse({"type": type_, "message": message}, status_code=status)


# ---------- pricing in Expedia's shape ----------

def money(x):
    return f"{x:.2f}"


def amount(cur, v):
    return {"value": money(v), "currency": cur}


def both(cur, v):
    return {"request_currency": amount(cur, v), "billable_currency": amount(cur, v)}


def room_pricing(price, nights, cur):
    """Pricing for ONE room of one occupancy (the workers multiply by rooms per occupancy)."""
    tax = round(price * TAX_PCT / 100, 2)
    nightly = [[{"type": "base_rate", **amount(cur, price)},
                {"type": "tax_and_service_fee", **amount(cur, tax)}] for _ in range(nights)]
    exclusive = round(price * nights, 2)
    inclusive = round((price + tax) * nights, 2)
    return {
        "nightly": nightly,
        "stay": [],
        "totals": {
            "inclusive": both(cur, inclusive),
            "exclusive": both(cur, exclusive),
            "inclusive_strikethrough": both(cur, inclusive),
            "strikethrough": both(cur, exclusive),
            "marketing_fee": both(cur, 0),
            "gross_profit": both(cur, 0),
        },
    }


def occupancy_pricing(t, price):
    nights = (date.fromisoformat(t["out"]) - date.fromisoformat(t["in"])).days or 1
    return {occ: room_pricing(price, nights, t["cur"]) for occ in dict.fromkeys(t["occ"])}


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def cancel_penalties(t):
    check_in = datetime.fromisoformat(t["in"]).replace(hour=14, tzinfo=timezone.utc)
    if is_nrf(t["pid"]):
        created = datetime.fromtimestamp(t["ts"], timezone.utc)
        return [{"start": fmt_ts(created), "end": fmt_ts(check_in), "percent": "100%", "currency": t["cur"]}]
    return [{"start": fmt_ts(check_in - timedelta(days=2)), "end": fmt_ts(check_in),
             "nights": "1", "currency": t["cur"]}]


def price_for(t, after_check):
    p = float(t["price"])
    if after_check and prop(t["pid"])["scenario"] == "PRICE_CHANGED":
        p = round(p * (1 + PRICE_CHANGE_PCT / 100), 2)
    return p


def rate_block(t, after_check=False):
    p = price_for(t, after_check)
    return {
        "id": t["rate"],
        "status": "available",
        "available_rooms": 10,
        "refundable": not is_nrf(t["pid"]),
        "member_deal_available": False,
        "sale_scenario": {"package": False, "member": False, "corporate": False, "distribution": False},
        "merchant_of_record": "expedia",
        "amenities": {},
        "cancel_penalties": cancel_penalties(t),
        "occupancy_pricing": occupancy_pricing(t, p),
    }


# ---------- app ----------

app = FastAPI(title="Expedia test supplier (fake)")


@app.middleware("http")
async def log_and_auth(request: Request, call_next):
    path = request.url.path
    raw = await request.body()
    if STRICT_AUTH and path.startswith("/v3"):
        m = re.search(r"APIKey=([^,]+),Signature=([^,]+),timestamp=(\d+)", request.headers.get("authorization", ""))
        ok = bool(m) and m.group(1) == API_KEY and abs(time.time() - int(m.group(3))) < 300 and \
            m.group(2) == hashlib.sha512(f"{API_KEY}{SHARED_SECRET}{m.group(3)}".encode()).hexdigest()
        if not ok:
            return err(401, "request_unauthenticated", "Data required to authenticate your request is missing or invalid.")
    if ADMIN_TOKEN and path.startswith("/__admin") and request.headers.get("x-admin-token") != ADMIN_TOKEN:
        return err(401, "request_unauthenticated", "Missing or wrong X-Admin-Token header.")
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": request.method,
             "path": path, "query": str(request.url.query)[:2000], "body": raw.decode(errors="ignore")[:4000]}
    LOG.append(entry)
    resp = await call_next(request)
    entry["status"] = resp.status_code
    return resp


class CollapseSlashes:
    """Credentials use base_url like https://test.ean.com//v3, so paths can arrive as //v3/..."""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, path=re.sub(r"/{2,}", "/", scope["path"]))
        await self.inner(scope, receive, send)


# ---------- shopping ----------

def availability(request: Request, only=None):
    q = request.query_params
    pids = [only] if only else q.getlist("property_id")
    occ = q.getlist("occupancy") or ["2"]
    cur = q.get("currency", "USD")
    check_in, check_out = q.get("checkin"), q.get("checkout")
    out = []
    for pid in pids:
        p = prop(pid)
        room_id, rate_id = "2" + pid[-6:], "3" + pid[-6:]
        t = {"pid": pid, "room": room_id, "rate": rate_id, "in": check_in, "out": check_out,
             "occ": occ, "cur": cur, "price": p["price"], "ts": int(time.time()), "n": uuid.uuid4().hex[:6]}
        rate = rate_block(t)
        rate["bed_groups"] = {"37620": {
            "id": "37620", "description": "1 King Bed",
            "links": {"price_check": {"method": "GET",
                                      "href": f"/v3/properties/{pid}/rooms/{room_id}/rates/{rate_id}?token={enc(t)}"}},
            "configuration": [{"type": "KingBed", "size": "King", "quantity": 1}],
        }}
        out.append({
            "property_id": pid, "status": "available", "score": 1000,
            "rooms": [{"id": room_id, "room_name": f"{label(pid)} - Double Room", "rates": [rate]}],
        })
    return out


@app.get("/v3/properties/availability")
async def shop(request: Request):
    return availability(request)


@app.get("/v3/properties/{pid}/availability")
async def shop_one(pid: str, request: Request):
    return availability(request, only=pid)


@app.get("/v3/properties/{pid}/rooms/{room_id}/rates/{rate_id}")
async def price_check(pid: str, room_id: str, rate_id: str, token: str):
    t = dec(token)
    s = prop(pid)["scenario"]
    if s == "SOLD_OUT":
        return {"status": "sold_out"}
    rate = rate_block(t, after_check=True)
    book_token = enc({**t, "price": price_for(t, True), "checked": True})
    return {
        "status": "available",
        "occupancy_pricing": rate["occupancy_pricing"],
        "cancel_penalties": rate["cancel_penalties"],
        "refundable": rate["refundable"],
        "links": {"book": {"method": "POST", "href": f"/v3/itineraries?token={book_token}"}},
    }


# ---------- booking ----------

def build_itinerary(t, body, itin_id):
    rooms = []
    occs = list(t["occ"])
    for i, r in enumerate(body.get("rooms", [])):
        rid = uuid.uuid4().hex
        occ = occs[i] if i < len(occs) else occs[0]
        adults, _, kids = occ.partition("-")
        rate = rate_block({**t, "occ": [occ]})
        rooms.append({
            "id": rid,
            "confirmation_id": {"expedia": str(itin_id)[-8:] + str(i), "property": f"MOCK{i}"},
            "bed_group_id": "37620",
            "checkin": t["in"], "checkout": t["out"],
            "number_of_adults": int(adults),
            "child_ages": [int(k) for k in kids.split(",")] if kids else [],
            "given_name": r.get("given_name", ""), "family_name": r.get("family_name", ""),
            "status": "held",
            "special_request": r.get("special_request", ""), "smoking": r.get("smoking", False),
            "rate": {
                "id": t["rate"], "merchant_of_record": "expedia",
                "refundable": not is_nrf(t["pid"]),
                "current_refundability": "non_refundable" if is_nrf(t["pid"]) else "refundable",
                "cancel_refund": amount(t["cur"], 0 if is_nrf(t["pid"]) else float(
                    rate["occupancy_pricing"][occ]["totals"]["inclusive"]["request_currency"]["value"])),
                "amenities": {},
                "cancel_penalties": rate["cancel_penalties"],
                "pricing": rate["occupancy_pricing"][occ],
            },
            "links": {"cancel": {"method": "DELETE",
                                 "href": f"/v3/itineraries/{itin_id}/rooms/{rid}?token={t['n']}{rid[:6]}"}},
        })
    return {
        "itinerary_id": str(itin_id),
        "property_id": t["pid"],
        "affiliate_reference_id": body.get("affiliate_reference_id", ""),
        "email": body.get("email", ""),
        "creation_date_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "rooms": rooms,
        "billing_contact": (body.get("payments") or [{}])[0].get("billing_contact", {}),
        "links": {"cancel": {"method": "DELETE", "href": f"/v3/itineraries/{itin_id}?token={t['n']}"}},
        "_token": t,
    }


def public(itin):
    return {k: v for k, v in itin.items() if not k.startswith("_")}


def set_status(itin, status):
    for r in itin["rooms"]:
        r["status"] = status


@app.post("/v3/itineraries")
async def hold(request: Request, token: str):
    t = dec(token)
    body = await request.json()
    s = prop(t["pid"])["scenario"]
    if s == "ERROR":
        return err(409, "rooms_unavailable", "One or more requested rooms are unavailable.")
    if s == "PRICE_MISMATCH":
        return err(409, "price_mismatch", "Payment amount did not match current price, please check price and try again.")
    if s == "TIMEOUT_NOT_BOOKED":
        await asyncio.sleep(TIMEOUT_SLEEP)
        return err(503, "service_unavailable", "Mock timeout, nothing booked.")
    itin_id = f"{int(time.time() * 1000) % 10**10:010d}{uuid.uuid4().int % 1000:03d}"
    itin = build_itinerary(t, body, itin_id)
    ITINERARIES[itin_id] = itin
    links = {
        "retrieve": {"method": "GET", "href": f"/v3/itineraries/{itin_id}?token={t['n']}r"},
        "resume": {"method": "PUT", "href": f"/v3/itineraries/{itin_id}?token={t['n']}h"},
        "cancel": {"method": "DELETE", "href": f"/v3/itineraries/{itin_id}?token={t['n']}h"},
    }
    return JSONResponse({"itinerary_id": itin_id, "links": links,
                         "affiliate_reference_id": itin["affiliate_reference_id"],
                         "creation_date_time": itin["creation_date_time"]}, status_code=201)


@app.put("/v3/itineraries/{itin_id}")
async def resume(itin_id: str):
    itin = ITINERARIES.get(itin_id)
    if not itin:
        return err(404, "resource.not_found", "The requested resource could not be found.")
    s = prop(itin["property_id"])["scenario"]
    if s == "SLOW_CONFIRM":
        await asyncio.sleep(SLOW_SLEEP)
    if s == "PENDING":
        set_status(itin, "pending")
    else:
        set_status(itin, "booked")
    if s in ("TIMEOUT_BOOKED_REFUNDABLE", "TIMEOUT_BOOKED_NRF"):
        await asyncio.sleep(TIMEOUT_SLEEP)   # booked at the supplier, but the worker gives up first
    return Response(status_code=204)


@app.get("/v3/itineraries/{itin_id}")
async def retrieve(itin_id: str):
    itin = ITINERARIES.get(itin_id)
    if not itin:
        return err(404, "resource.not_found", "The requested resource could not be found.")
    return public(itin)


@app.get("/v3/itineraries")
async def retrieve_by_reference(affiliate_reference_id: str = "", email: str = ""):
    found = [public(i) for i in ITINERARIES.values()
             if i["affiliate_reference_id"] == affiliate_reference_id and (not email or i["email"] == email)]
    if not found:
        return err(404, "resource.not_found", "The requested resource could not be found.")
    return found


@app.delete("/v3/itineraries/{itin_id}")
async def cancel_held(itin_id: str):
    itin = ITINERARIES.get(itin_id)
    if not itin:
        return err(404, "resource.not_found", "The requested resource could not be found.")
    set_status(itin, "canceled")
    return Response(status_code=204)


@app.delete("/v3/itineraries/{itin_id}/rooms/{room_id}")
async def cancel_room(itin_id: str, room_id: str):
    itin = ITINERARIES.get(itin_id)
    room = next((r for r in (itin or {}).get("rooms", []) if r["id"] == room_id), None)
    if not room:
        return err(404, "resource.not_found", "The requested resource could not be found.")
    if room["status"] == "canceled":
        return err(400, "room_already_cancelled", "Room has already been cancelled.")
    if is_nrf(itin["property_id"]) and NRF_CANCEL == "refuse":
        return err(400, "cancel.not_allowed", "This room is non-refundable and cannot be cancelled.")
    room["status"] = "canceled"
    return Response(status_code=204)


# ---------- admin (dev only) ----------

@app.get("/__admin")
async def admin():
    return {
        "default_scenario_for_other_properties": DEFAULT_SCENARIO,
        "scenarios": SCENARIOS,
        "properties": {pid: {**p, "label": label(pid)} for pid, p in PROPERTIES.items()},
        "itineraries": [{"itinerary_id": i["itinerary_id"], "property_id": i["property_id"],
                         "affiliate_reference_id": i["affiliate_reference_id"],
                         "rooms": [{"id": r["id"], "status": r["status"]} for r in i["rooms"]]}
                        for i in ITINERARIES.values()],
    }


@app.post("/__admin/properties/{pid}")
async def set_property(pid: str, request: Request):
    """Set the scenario (and optionally price) of any property id, including real mapped ones."""
    b = await request.json()
    if b.get("scenario") not in SCENARIOS:
        return err(400, "invalid_input", f"scenario must be one of {list(SCENARIOS)}")
    PROPERTIES[pid] = {"scenario": b["scenario"], "price": b.get("price", prop(pid)["price"])}
    return {pid: {**PROPERTIES[pid], "label": label(pid)}}


@app.post("/__admin/itineraries/{key}/status")
async def set_itinerary_status(key: str, request: Request):
    """key = itinerary_id or affiliate_reference_id; status = booked | canceled | pending."""
    status = (await request.json()).get("status")
    if status not in ("booked", "canceled", "pending"):
        return err(400, "invalid_input", "status must be booked, canceled or pending")
    hits = [i for i in ITINERARIES.values() if key in (i["itinerary_id"], i["affiliate_reference_id"])]
    if not hits:
        return err(404, "resource.not_found", "No itinerary with that id or reference")
    for i in hits:
        set_status(i, status)
    return [public(i) for i in hits]


@app.get("/__admin/log")
async def admin_log(path: str = "", limit: int = 100):
    return [e for e in LOG if path in e["path"]][-limit:]


@app.post("/__admin/reset")
async def admin_reset():
    reset_state()
    return {"ok": True}


app = CollapseSlashes(app)
