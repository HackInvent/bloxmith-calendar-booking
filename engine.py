"""Proposal → exact local draft → approval → one durable event-creation attempt."""

import asyncio
from datetime import datetime,timedelta,timezone
import html
import sqlite3
import time
import uuid
from . import availability,network,policy
from .journal import Journal

RESERVED = {"creating","uncertain","confirmed","external_change"}


def receipt(record, duplicate=False):
    phase = record["phase"]
    if phase=="creating":
        phase="uncertain"
    elif phase=="searching":
        phase="interrupted"
    result = {"request_id":record["request_id"],"status":phase,"duplicate":duplicate,
              "uncertain":phase in {"uncertain","external_change"}}
    if record.get("draft"):
        result.update(draft_id=record["draft"]["draft_id"],revision=record["draft"]["revision"])
        if record["phase"] in RESERVED:
            result["attempted_event_id"]=record["draft"]["event_id"]
    result.update(record.get("result",{}))
    return result


def preview(record, cfg, slot):
    selected=set(record["request"]["participants"])
    attendees = [person["email"] for person in cfg["participants"] if person["id"] in selected]
    request=record["request"]
    draft={"request_id":record["request_id"],"draft_id":uuid.uuid4().hex,
        "slot":slot,"summary":request["summary"],"description":request["description"],"location":request["location"],
        "attendees":sorted(attendees),"calendar_id":cfg["calendar_id"],"credential_ref":cfg["credential_ref"],
        "event_id":record["event_id"],"send_updates":"all","expires_at":record["expires_at"],
        "scope_sha256":policy.digest(policy.scope(cfg)),"availability_calendars":availability.calendars(request,cfg)}
    draft["revision"]=policy.digest(draft)
    return draft


def event_body(draft):
    return {"id":draft["event_id"],"summary":draft["summary"],"description":html.escape(draft["description"]),
        "location":draft["location"],"start":{"dateTime":draft["slot"]["start"],"timeZone":draft["slot"]["timezone"]},
        "end":{"dateTime":draft["slot"]["end"],"timeZone":draft["slot"]["timezone"]},
        "attendees":[{"email":address} for address in draft["attendees"]],
        "transparency":"opaque","visibility":"private","guestsCanInviteOthers":False,"guestsCanModify":False,
        "guestsCanSeeOtherGuests":True,"reminders":{"useDefault":False},
        "extendedProperties":{"private":{"bloxsmithApproval":draft["revision"]}}}


def matches(event, draft):
    """Verify actual provider state; approval hashes alone cannot hide a changed event."""
    try:
        expected=event_body(draft)
        if not isinstance(event,dict) or event.get("id")!=draft["event_id"] or event.get("status")!="confirmed" or event.get("attendeesOmitted"):
            return False
        for name in ("summary","description","location"):
            if event.get(name,"")!=expected[name]:
                return False
        for name in ("start","end"):
            if policy.instant(event[name]["dateTime"])!=policy.instant(expected[name]["dateTime"]):
                return False
        people=event.get("attendees",[])
        if not isinstance(people,list) or sorted(policy.email(person["email"]) for person in people)!=draft["attendees"]:
            return False
        if event.get("transparency","opaque")!="opaque" or event.get("recurrence"):
            return False
        return event.get("extendedProperties",{}).get("private",{}).get("bloxsmithApproval")==draft["revision"]
    except (ValueError,KeyError,TypeError,AttributeError):
        return False


def ensure_scope(record, cfg):
    if record["scope_sha256"]!=policy.digest(policy.scope(cfg)):
        policy.fail("Calendar, participants or availability settings changed; prepare a new request.","scope_changed")


def local_reservations(journal, cfg, excluded=None, ids=None):
    intervals=[]
    ids=set(ids or [cfg["calendar_id"]])
    for record in journal.all():
        draft=record.get("draft")
        if record["request_id"]!=excluded and record["phase"] in RESERVED and draft and ids.intersection(draft["availability_calendars"]):
            intervals.append((policy.instant(draft["slot"]["start"]),policy.instant(draft["slot"]["end"])))
    return intervals


def saved_outcome(journal, record):
    try:
        journal.save(record)
    except (OSError,sqlite3.Error,policy.CalendarError):
        # A durable 'creating' row already exists. Never clear it or repeat insertion.
        result=receipt(record)
        result["journal_warning"]="Outcome persistence failed; the reserved creation attempt will not be replayed."
        return {"receipt":result}
    return {"receipt":receipt(record)}


async def search(client, journal, record, cfg):
    request=record["request"]
    now=datetime.now(timezone.utc)
    # Validate future horizon before transmitting a query.
    availability.slots(request,cfg,[],now)
    margin=timedelta(minutes=cfg["buffer_minutes"])
    start,end=policy.instant(request["period"]["start"])-margin,policy.instant(request["period"]["end"])+margin
    ids=availability.calendars(request,cfg)
    await client.authorize()
    busy=availability.busy_intervals(await client.freebusy(ids,start,end),ids,start,end)
    busy.extend(local_reservations(journal,cfg,ids=ids))
    options=availability.slots(request,cfg,busy,datetime.now(timezone.utc))
    record.update(phase="proposed" if options else "no_slots", options=options,
        expires_at=time.time()+cfg["draft_lifetime_sec"])
    journal.save(record)
    return {"slots":{"request_id":record["request_id"],"status":record["phase"],"options":options,
        "expires_at":record["expires_at"],"timezone":cfg["timezone"],"reservation":False},"receipt":receipt(record)}


async def approve(client,journal,record,cfg):
    draft=record["draft"]
    start,end=policy.instant(draft["slot"]["start"]),policy.instant(draft["slot"]["end"])
    if start<datetime.now(timezone.utc)+timedelta(minutes=cfg["min_notice_minutes"]):
        record["phase"]="expired"; journal.save(record)
        return {"receipt":receipt(record)}
    margin=timedelta(minutes=cfg["buffer_minutes"])
    ids=draft["availability_calendars"]
    await client.authorize()
    busy=availability.busy_intervals(await client.freebusy(ids,start-margin,end+margin),ids,start-margin,end+margin)
    busy.extend(local_reservations(journal,cfg,record["request_id"],ids))
    if availability.overlaps(start,end,busy,cfg["buffer_minutes"]):
        record["phase"]="conflict"; journal.save(record)
        return {"receipt":receipt(record)}
    network.check(client.deadline,client.cancel)
    if time.time()>=draft["expires_at"] or start<datetime.now(timezone.utc)+timedelta(minutes=cfg["min_notice_minutes"]):
        record["phase"]="expired"; journal.save(record)
        return {"receipt":receipt(record)}
    record.update(phase="creating",attempted_at=time.time())
    journal.save(record)  # Durable before the first and only event insertion for this request.
    try:
        code,event=await client.insert(draft["calendar_id"],event_body(draft))
        if code in (200,201) and matches(event,draft):
            record["phase"]="confirmed"
            record["result"]={"event_id":draft["event_id"],"calendar_id":draft["calendar_id"],
                "start":draft["slot"]["start"],"end":draft["slot"]["end"],"invitations_requested":True,
                "attendee_acceptance_confirmed":False}
        elif code in (400,401,403,404,405,413,422,429):
            record["phase"]="provider_rejected"
            record["result"]={"http_status":code,"detail":"Calendar rejected creation; this request will not be resubmitted."}
        else:
            record["phase"]="uncertain"
            record["result"]={"http_status":code,"detail":"Creation was not conclusively confirmed; reconcile this event ID without resubmitting."}
    except Exception as exc:
        record["phase"]="uncertain"
        record["result"]={"code":exc.code if isinstance(exc,policy.CalendarError) else "unavailable",
            "detail":"Creation outcome is unknown; no automatic retry. Use reconcile with this request ID."}
    return saved_outcome(journal,record)


async def reconcile(client,journal,record):
    draft=record["draft"]
    await client.authorize()
    code,event=await client.event(draft["calendar_id"],draft["event_id"])
    if code==200 and matches(event,draft):
        record["phase"]="confirmed"
        record["result"]={"event_id":draft["event_id"],"calendar_id":draft["calendar_id"],
            "start":draft["slot"]["start"],"end":draft["slot"]["end"],"reconciled":True,"attendee_acceptance_confirmed":False}
    elif code==410 or (code==200 and event.get("id")==draft["event_id"] and event.get("status")=="cancelled"):
        record["phase"]="removed"
        record["result"]={"event_id":draft["event_id"],"detail":"The provider reports removal; this request will never recreate it."}
    elif code==200:
        record["phase"]="external_change"
        record["result"]={"event_id":draft["event_id"],"detail":"The event no longer matches the exact approved content; review it in Calendar."}
    else:
        record["phase"]="uncertain"
        record["result"]={"event_id":draft["event_id"],"http_status":code,
            "detail":"Event existence could not be confirmed; even a 404 never authorizes a second insertion."}
    return saved_outcome(journal,record)


def process(context,port,raw):
    cfg=policy.configuration(context.config)
    data=policy.message(raw,port,cfg)
    resolver=context.services.get("get_block_storage_dir")
    if not callable(resolver):
        policy.fail("The block storage service is unavailable.","storage")
    cancel=context.services.get("cancel_requested",lambda:False)
    with Journal(resolver(),cfg) as journal:
        record=journal.get(data["request_id"])
        if record and record["phase"] in {"proposed","draft"} and time.time()>=record["expires_at"]:
            record["phase"]="expired"; journal.save(record)
        if port=="decision":
            if not record or not record.get("draft"):
                policy.fail("Unknown draft.","draft")
            draft=record["draft"]
            if data["draft_id"]!=draft["draft_id"] or data["revision"]!=draft["revision"] or policy.digest({k:v for k,v in draft.items() if k!="revision"})!=draft["revision"]:
                policy.fail("Decision does not match the exact reviewed draft.","conflict")
            if record["phase"]!="draft":
                return {"receipt":receipt(record,True)}
            if data["decision"]=="reject":
                record["phase"]="rejected"; journal.save(record)
                return {"receipt":receipt(record)}
            if not cfg["enabled"] or not cfg["booking_enabled"]:
                policy.fail("Creation is disabled; the draft remains pending.","disabled")
            ensure_scope(record,cfg)
            if time.time()>=draft["expires_at"]:
                record["phase"]="expired"; journal.save(record)
                return {"receipt":receipt(record)}
            action=lambda client:approve(client,journal,record,cfg)
        elif data["operation"]=="status":
            if not record:
                policy.fail("Unknown request ID.")
            return {"receipt":receipt(record)}
        elif data["operation"]=="reconcile":
            if not record or record["phase"] not in RESERVED or not record.get("draft"):
                policy.fail("Reconciliation needs a prior creation attempt.")
            if not cfg["enabled"]:
                policy.fail("Remote calendar access is disabled.","disabled")
            if record["draft"]["calendar_id"]!=cfg["calendar_id"] or record["draft"]["credential_ref"]!=cfg["credential_ref"]:
                policy.fail("Reconciliation must use the original calendar and wallet reference.","scope_changed")
            action=lambda client:reconcile(client,journal,record)
        elif data["operation"]=="draft":
            if not record or record["phase"] not in {"proposed","draft"}:
                policy.fail("Select a slot from an outstanding search request.","draft")
            ensure_scope(record,cfg)
            if time.time()>=record["expires_at"]:
                record["phase"]="expired"; journal.save(record)
                return {"receipt":receipt(record)}
            slot=next((slot for slot in record["options"] if slot["slot_id"]==data["slot_id"]),None)
            if slot is None:
                policy.fail("Slot ID does not belong to this request.","draft")
            if record.get("draft",{}).get("slot",{}).get("slot_id")!=slot["slot_id"]:
                record.update(phase="draft",draft=preview(record,cfg,slot)); journal.save(record)
            return {"draft":record["draft"],"receipt":receipt(record)}
        else:
            if record:
                if record["request_digest"]!=policy.digest(data):
                    policy.fail("Request ID already belongs to different content.","conflict")
                result={"receipt":receipt(record,True)}
                if record["phase"] in {"proposed","no_slots"}:
                    result["slots"]={"request_id":record["request_id"],"options":record["options"],"status":record["phase"],
                        "expires_at":record["expires_at"],"cached":True,"reservation":False}
                elif record["phase"]=="draft":
                    result["draft"]=record["draft"]
                return result
            if not cfg["enabled"] or not cfg["calendar_id"]:
                policy.fail("Enable remote access and configure an explicit calendar ID.","disabled")
            record={"request_id":data["request_id"],"request":data,"request_digest":policy.digest(data),
                "phase":"searching","scope_sha256":policy.digest(policy.scope(cfg)),"event_id":uuid.uuid4().hex}
            action=lambda client:search(client,journal,record,cfg)
        credentials=policy.credentials(context,cfg)
        if record["phase"]=="searching":
            journal.save(record)
        async def run():
            async with network.Client(credentials,cfg["timeout_sec"],cancel) as client:
                return await action(client)
        try:
            return asyncio.run(run())
        except Exception:
            if record["phase"]=="searching":
                record["phase"]="search_failed"; journal.save(record)
            raise
