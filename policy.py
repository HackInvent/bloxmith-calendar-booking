"""Strict settings, request scopes, wallet credentials and approval fingerprints."""

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULTS = {"enabled": False, "booking_enabled": False, "credential_ref": "", "calendar_id": "",
    "timezone": "Europe/Paris", "participants": [],
    "business_hours": {day: [["09:00", "17:00"]] for day in DAYS[:5]},
    "horizon_days": 30, "step_minutes": 15, "buffer_minutes": 10, "min_notice_minutes": 30,
    "max_participants": 20, "max_slots": 10, "max_duration_minutes": 240,
    "draft_lifetime_sec": 900, "timeout_sec": 30, "max_records": 1000, "max_storage_bytes": 33554432}


class CalendarError(ValueError):
    def __init__(self, detail, code="invalid", *, uncertain=False):
        super().__init__(detail)
        self.code, self.uncertain = code, uncertain


def fail(detail, code="invalid", **kw):
    raise CalendarError(detail, code, **kw)


def encoded(value, limit=1048576):
    try:
        raw = json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > limit:
            raise ValueError()
        return raw.decode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail("Expected bounded JSON data.")


def plain(value, depth=0):
    if depth>32:
        fail("JSON nesting exceeds its limit.")
    if isinstance(value,Mapping):
        if any(not isinstance(key,str) for key in value):
            fail("JSON object keys must be strings.")
        return {key:plain(item,depth+1) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [plain(item,depth+1) for item in value]
    return value


def document(value, limit=65536):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError()
            result[key] = item
        return result
    try:
        if isinstance(value, Mapping):
            value = plain(value)
            encoded(value, limit)
        else:
            if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
                raise ValueError()
            value = json.loads(value, object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        fail("Expected one bounded JSON object without duplicate keys.")


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def line(value, limit=1024, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not value.strip() and not empty) or any(ord(c)<32 for c in value):
        fail("Expected bounded text without control characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        fail("Invalid text encoding.")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        fail("Use a request/participant ID with 1–128 ASCII letters, digits, underscores or hyphens.")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        fail(f"Expected an integer between {low} and {high}.")
    return value


def calendar_id(value, empty=False):
    value = line(value, 1024, empty)
    if value and (value == "primary" or value in {".", ".."} or not value.isascii() or any(c.isspace() for c in value) or "/" in value):
        fail("Use an explicit Google calendar ID, not primary, a group alias or a URL.")
    return value


def email(value):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        fail("Use an explicit ASCII participant email address.")
    return value.lower()


def minute(value, end=False):
    if end and value == "24:00":
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        fail("Business hours use HH:MM, with 24:00 allowed only as an end.")
    return int(value[:2])*60 + int(value[3:])


def configuration(raw):
    if not isinstance(raw, Mapping) or set(raw)-set(DEFAULTS)-{"execution", "runtime_path", "runtime_path_label"}:
        fail("Unknown setting; OAuth credentials belong in the wallet.")
    cfg = {**DEFAULTS, **{key: value for key, value in raw.items() if key in DEFAULTS}}
    for name in ("enabled", "booking_enabled"):
        if type(cfg[name]) is not bool:
            fail("Enable choices must be booleans.")
    cfg["calendar_id"] = calendar_id(cfg["calendar_id"], empty=True)
    reference = line(cfg["credential_ref"], 512, empty=True)
    if reference and not reference.startswith("secret://"):
        fail("Use a secret:// wallet reference, never an OAuth token.")
    try:
        ZoneInfo(line(cfg["timezone"], 128))
    except (ZoneInfoNotFoundError, ValueError):
        fail("Choose an installed IANA time zone.")
    bounds = {"horizon_days": (1,90), "step_minutes": (1,60), "buffer_minutes": (0,240),
        "min_notice_minutes": (0,10080), "max_participants": (1,40), "max_slots": (1,50),
        "max_duration_minutes": (1,480), "draft_lifetime_sec": (30,86400), "timeout_sec": (1,120),
        "max_records": (1,10000), "max_storage_bytes": (1048576,134217728)}
    for name, limits in bounds.items():
        integer(cfg[name], *limits)
    people = cfg["participants"]
    if not isinstance(people, (list, tuple)) or len(people)>40:
        fail("Participants must be a JSON array with at most 40 configured people.")
    result, ids, addresses = [], set(), set()
    for person in people:
        if not isinstance(person, Mapping) or set(person) != {"id", "email", "calendar_id"}:
            fail("Each participant needs exactly id, email and calendar_id.")
        item = {"id": identifier(person["id"]), "email": email(person["email"]), "calendar_id": calendar_id(person["calendar_id"])}
        if item["id"] in ids or item["email"] in addresses:
            fail("Participant IDs and email addresses must be distinct.")
        ids.add(item["id"]); addresses.add(item["email"]); result.append(item)
    cfg["participants"] = result
    hours = cfg["business_hours"]
    if not isinstance(hours, Mapping) or not hours or set(hours)-set(DAYS):
        fail("Business hours map mon–sun to arrays of [start,end] intervals.")
    normalized = {}
    for day, spans in hours.items():
        if not isinstance(spans, (list, tuple)) or len(spans)>4:
            fail("Configure at most four non-overlapping intervals per day.")
        ordered = []
        for span in spans:
            if not isinstance(span, (list, tuple)) or len(span)!=2 or minute(span[0]) >= minute(span[1], True):
                fail("Business intervals must increase within one local day; split overnight periods.")
            ordered.append(list(span))
        ordered.sort()
        if any(minute(a[1], True)>minute(b[0]) for a,b in zip(ordered,ordered[1:])):
            fail("Business-hour intervals cannot overlap.")
        normalized[day] = ordered
    if not any(normalized.values()):
        fail("Configure at least one working interval.")
    cfg["business_hours"] = normalized
    return cfg


def instant(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)", value):
        fail("Times require RFC3339 with an explicit UTC offset.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        if not 1971 <= result.year <= 9998:
            raise ValueError()
        return result
    except (ValueError, OverflowError):
        fail("Invalid timestamp.")


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def scope(cfg):
    return {name: cfg[name] for name in ("calendar_id", "credential_ref", "timezone", "participants", "business_hours",
        "buffer_minutes", "min_notice_minutes", "step_minutes", "max_duration_minutes", "horizon_days", "max_participants")}


def message(raw, port, cfg):
    data = document(raw)
    identifier(data.get("request_id"))
    if port == "decision":
        if set(data) != {"request_id", "draft_id", "revision", "decision"} or data["decision"] not in ("approve", "reject"):
            fail("Decision requires request_id, draft_id, revision and approve/reject.")
        for key, size in (("draft_id",32), ("revision",64)):
            if not isinstance(data[key], str) or not re.fullmatch(r"[0-9a-f]{"+str(size)+r"}", data[key]):
                fail("Invalid draft identity or revision.")
        return data
    if port != "request":
        fail("Unknown input port.")
    operation = data.get("operation")
    allowed = {"search": {"participants", "period", "duration_minutes", "summary", "description", "location"},
        "draft": {"slot_id"}, "status": set(), "reconcile": set()}
    if not isinstance(operation, str) or operation not in allowed or set(data)-{"request_id","operation"}-allowed[operation]:
        fail("Unknown request operation or extra fields.")
    if operation == "draft":
        identifier(data.get("slot_id"))
    if operation == "search":
        period = data.get("period")
        if not isinstance(period, Mapping) or set(period)!={"start","end"}:
            fail("Search requires period.start and period.end.")
        start, end = instant(period["start"]), instant(period["end"])
        if not 0 < (end-start).total_seconds() <= cfg["horizon_days"]*86400:
            fail("Search period exceeds the configured horizon.")
        data["period"] = {"start": stamp(start), "end": stamp(end)}
        integer(data.get("duration_minutes"), 1, cfg["max_duration_minutes"])
        selected = data.get("participants", [])
        if not isinstance(selected, (list,tuple)) or len(selected)>cfg["max_participants"]:
            fail("Too many requested participants.")
        selected = [identifier(value) for value in selected]
        if len(set(selected))!=len(selected) or set(selected)-{person["id"] for person in cfg["participants"]}:
            fail("Use distinct participant IDs from the configured allowlist.", "scope")
        data["participants"] = sorted(selected)
        data["summary"] = line(data.get("summary"), 256)
        data["location"] = line(data.get("location", ""), 512, True)
        description = data.get("description", "")
        if not isinstance(description,str) or len(description)>4000 or any(ord(c)<32 and c not in "\n\t" for c in description):
            fail("Description must be bounded plain text.")
        encoded(description, 24000)
        data["description"] = description
    return data


def credentials(context, cfg):
    try:
        resolver = context.services.get("resolve_secret")
        if not cfg["credential_ref"] or not callable(resolver):
            raise ValueError()
        secret = document(resolver(cfg["credential_ref"]), 32768)
        if set(secret) not in ({"access_token"}, {"client_id","client_secret","refresh_token"}):
            raise ValueError()
        for value in secret.values():
            if not isinstance(value,str) or not 1<=len(value)<=8192 or any(ord(c)<33 or ord(c)>126 for c in value):
                raise ValueError()
        return secret
    except Exception:
        fail("Unlock the wallet and provide OAuth JSON: access_token OR client_id/client_secret/refresh_token.", "secret")
