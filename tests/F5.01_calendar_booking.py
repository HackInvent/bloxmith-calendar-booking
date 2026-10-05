"""FB1/FB2/FB3/FB4/FB5: timezone maths, real HTTP, exact approval and durable no-resubmit receipts."""

from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType,SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"tests"),str(Path(__file__).parent)]
from blocs.calendar_booking import availability,engine,network,policy
from blocs.calendar_booking.journal import Journal
from calendar_fixture import provider,settings,request,SECRET,TOKEN,REF


def refused(action,code=None):
    try:
        action()
    except policy.CalendarError as exc:
        assert not code or exc.code==code,(exc.code,str(exc))
        return exc
    raise AssertionError("Expected controlled refusal")


def context(path,cfg,cancel=lambda:False,secret=SECRET):
    path.mkdir(exist_ok=True,parents=True)
    return SimpleNamespace(config=cfg,services={"get_block_storage_dir":lambda:path,
        "resolve_secret":lambda ref:secret,"cancel_requested":cancel})


def prepare(ctx,ident="meeting"):
    found=engine.process(ctx,"request",request(ident))["slots"]
    assert found["options"]
    return engine.process(ctx,"request",{"request_id":ident,"operation":"draft","slot_id":found["options"][0]["slot_id"]})["draft"]


def approve(ctx,draft,**extra):
    return engine.process(ctx,"decision",{**{key:draft[key] for key in ("request_id","draft_id","revision")},"decision":"approve",**extra})["receipt"]


def timezones():
    cfg=policy.configuration({"timezone":"Europe/Paris","min_notice_minutes":0,"buffer_minutes":0,"step_minutes":30,
        "max_slots":50,"business_hours":{"sun":[["01:00","04:00"]]}})
    for start,end,expected in (("2026-03-29T00:00:00Z","2026-03-29T03:00:00Z",4),
        ("2026-10-24T23:00:00Z","2026-10-25T04:00:00Z",8)):
        data={"period":{"start":start,"end":end},"duration_minutes":30}
        items=availability.slots(data,cfg,[],policy.instant(start)-timedelta(days=1))
        assert len(items)==expected,(start,items)
        assert len({item["start"] for item in items})==expected
        if "03-29" in start:
            assert not any("T02:" in item["local_start"] for item in items)
        else:
            repeated=[item["local_start"] for item in items if "T02:00" in item["local_start"]]
            assert len(repeated)==2 and {text[-6:] for text in repeated}=={"+01:00","+02:00"}
    cfg=policy.configuration({**settings(),"buffer_minutes":10})
    data=request(); now=policy.instant(data["period"]["start"])-timedelta(days=1)
    beginning=policy.instant(data["period"]["start"])
    items=availability.slots(data,cfg,[(beginning,beginning+timedelta(minutes=30))],now)
    assert policy.instant(items[0]["start"])==beginning+timedelta(minutes=45)


def main():
    for bad in ({"token":"unsafe"},{"calendar_id":"primary"},{"timezone":"not/a/zone"},
        {"max_slots":True},{"business_hours":{"mon":[["23:00","02:00"]]}},{"credential_ref":"raw-token"}):
        refused(lambda:policy.configuration(bad))
    refused(lambda:policy.document('{"request_id":"a","request_id":"b"}'))
    assert policy.document(MappingProxyType({"nested":MappingProxyType({"x":(1,2)})}))["nested"]["x"]==[1,2]
    timezones()
    with provider() as api,TemporaryDirectory(prefix="calendar-real-http-") as temporary,\
        patch.object(network,"CALENDAR_ROOT",api.url+"/calendar/v3"),patch.object(network,"TOKEN_URL",api.url+"/token"):
        root=Path(temporary);cfg=policy.configuration(settings())
        ctx=context(root/"good",cfg)
        draft=prepare(ctx)
        assert not api.insertions,"Search and local draft sent an invitation"
        assert draft["attendees"]==["staff@example.test"] and draft["send_updates"]=="all"
        refused(lambda:approve(ctx,draft,revision="0"*64),"conflict")
        assert not api.insertions
        result=approve(ctx,draft)
        assert result["status"]=="confirmed" and not result["attendee_acceptance_confirmed"],result
        assert api.insertions[0]["description"]=="Plain &lt;text&gt;"
        assert approve(ctx,draft)["duplicate"] and len(api.insertions)==1
        assert engine.process(ctx,"request",request())["receipt"]["duplicate"]
        refused(lambda:engine.process(ctx,"request",request(summary="Changed")),"conflict")
        # Replay after recreating the context still uses durable receipts, not process memory.
        ctx=context(root/"good",dict(cfg))
        assert approve(ctx,draft)["status"]=="confirmed" and len(api.insertions)==1
        assert TOKEN.encode() not in (root/"good"/"calendar.sqlite3").read_bytes()
        for case in ("disabled","changed_scope","expired","reject","conflict","lost","rejected","crash","cancel"):
            api.mode="ok";api.busy=[]
            current=context(root/case,dict(cfg));prepared=prepare(current,case);before=len(api.insertions)
            if case=="disabled":
                current.config["booking_enabled"]=False
                refused(lambda:approve(current,prepared),"disabled")
            elif case=="changed_scope":
                current.config["buffer_minutes"]=11
                refused(lambda:approve(current,prepared),"scope_changed")
            elif case=="expired":
                with patch.object(engine.time,"time",return_value=prepared["expires_at"]+1):
                    assert approve(current,prepared)["status"]=="expired"
            elif case=="reject":
                assert approve(current,prepared,decision="reject")["status"]=="rejected"
            elif case=="conflict":
                api.busy=[{"start":prepared["slot"]["start"],"end":prepared["slot"]["end"]}]
                assert approve(current,prepared)["status"]=="conflict"
            elif case=="crash":
                with Journal(root/case,cfg) as journal:
                    record=journal.get(case);record["phase"]="creating";journal.save(record)
                assert approve(current,prepared)["status"]=="uncertain"
                read=engine.process(current,"request",{"request_id":case,"operation":"reconcile"})
                assert read["receipt"]["status"]=="uncertain"
                assert approve(current,prepared)["duplicate"]
            elif case=="rejected":
                api.mode="rejected"
                assert approve(current,prepared)["status"]=="provider_rejected"
                assert approve(current,prepared)["duplicate"]
            else:
                api.mode="hold" if case=="cancel" else "lost"
                if case=="cancel":
                    api.release.clear()
                    current.services["cancel_requested"]=lambda:len(api.insertions)>before
                started=time.monotonic();outcome=approve(current,prepared)
                assert outcome["status"]=="uncertain" and time.monotonic()-started<3,outcome
                assert len(api.insertions)==before+1 and approve(current,prepared)["duplicate"]
                current.services["cancel_requested"]=lambda:False;api.release.set();api.mode="ok"
                result=engine.process(current,"request",{"request_id":case,"operation":"reconcile"})["receipt"]
                assert result["status"]=="confirmed" and len(api.insertions)==before+1
                api.mode="changed"
                assert engine.process(current,"request",{"request_id":case,"operation":"reconcile"})["receipt"]["status"]=="external_change"
                api.mode="removed"
                assert engine.process(current,"request",{"request_id":case,"operation":"reconcile"})["receipt"]["status"]=="removed"
                assert approve(current,prepared)["duplicate"] and len(api.insertions)==before+1
                continue
            assert len(api.insertions)==before
        api.mode="ok";api.busy=[]
        oauth=json.dumps({"client_id":"fixture-client","client_secret":"fixture-client-secret","refresh_token":"fixture-refresh"})
        refresh=context(root/"refresh",cfg,secret=oauth)
        prepare(refresh,"refresh")
        assert any(call["path"]=="/token" for call in api.calls)
        assert b"fixture-refresh" not in (root/"refresh"/"calendar.sqlite3").read_bytes()
        for mode in ("redirect","oversized","unavailable"):
            api.mode=mode
            current=context(root/mode,cfg)
            refused(lambda:engine.process(current,"request",request(mode)))
        assert not any(call["path"]=="/must-not-follow" for call in api.calls)
        api.mode="ok"
        limited=context(root/"limited",{**cfg,"max_records":1})
        prepare(limited,"first")
        before=len(api.calls)
        refused(lambda:prepare(limited,"second"),"storage")
        assert len(api.calls)==before
        invalid=context(root/"secret",cfg,secret="sensitive-invalid-wallet-content")
        failure=refused(lambda:prepare(invalid),"secret")
        assert "sensitive" not in str(failure)
    print("[ok] Google protocol fixture, DST slots, exact approval, conflict recheck, durable no-resubmit, OAuth, Stop and reconciliation")


if __name__=="__main__":
    main()
