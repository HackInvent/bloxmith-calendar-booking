"""Elapsed-time slots with local working hours and explicit-offset DST handling."""

from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo
from . import policy


def calendars(request, cfg):
    selected = set(request["participants"])
    return sorted({cfg["calendar_id"], *[p["calendar_id"] for p in cfg["participants"] if p["id"] in selected]})


def busy_intervals(body, ids, start, end):
    if (not isinstance(body, dict) or body.get("kind")!="calendar#freeBusy" or body.get("groups")
        or not isinstance(body.get("calendars"),dict) or set(body["calendars"])!=set(ids)):
        policy.fail("Availability response is incomplete; unknown is not free.", "availability")
    if policy.instant(body.get("timeMin")) != start or policy.instant(body.get("timeMax")) != end:
        policy.fail("Availability response covers a different period.", "availability")
    intervals = []
    for ident in ids:
        record = body["calendars"][ident]
        if not isinstance(record,dict) or record.get("errors") or not isinstance(record.get("busy"),list) or len(record["busy"])>20000:
            policy.fail("A requested calendar is unavailable; its time cannot be treated as free.", "availability")
        for value in record["busy"]:
            if not isinstance(value,dict) or not {"start","end"} <= set(value):
                policy.fail("Invalid availability interval.", "availability")
            left, right = policy.instant(value["start"]), policy.instant(value["end"])
            if right<=left:
                policy.fail("Invalid availability interval.", "availability")
            intervals.append((left,right))
    return intervals


def overlaps(start, end, intervals, buffer_minutes):
    margin = timedelta(minutes=buffer_minutes)
    return any(start < right+margin and end > left-margin for left,right in intervals)


def slots(request, cfg, busy, now=None):
    now = now or datetime.now(timezone.utc)
    start, end = policy.instant(request["period"]["start"]), policy.instant(request["period"]["end"])
    if end>now+timedelta(days=cfg["horizon_days"]):
        policy.fail("Requested dates exceed the configured future horizon.")
    start = max(start, now+timedelta(minutes=cfg["min_notice_minutes"]))
    # UTC stepping cannot manufacture a nonexistent local time; both repeated local
    # times remain distinguishable by their explicit offset during a fall-back.
    base = math.ceil(start.timestamp()/60)*60
    count = max(0, int((end.timestamp()-base)//60))
    if count>cfg["horizon_days"]*1440+1:
        policy.fail("Too many candidate minutes.")
    margin = cfg["buffer_minutes"]*60
    delta = [0]*(count+1)
    for left,right in busy:
        a = max(0, min(count, math.floor((left.timestamp()-margin-base)/60)))
        b = max(0, min(count, math.ceil((right.timestamp()+margin-base)/60)))
        if a<b:
            delta[a]+=1; delta[b]-=1
    zone = ZoneInfo(cfg["timezone"])
    hours = {day: [(policy.minute(span[0]), policy.minute(span[1],True)) for span in spans]
             for day,spans in cfg["business_hours"].items()}
    invalid, eligible, active = [0], [], 0
    for index in range(count):
        active += delta[index]
        local = datetime.fromtimestamp(base+index*60, zone)
        minute = local.hour*60+local.minute
        working = any(a<=minute<b for a,b in hours.get(policy.DAYS[local.weekday()], []))
        eligible.append(minute%cfg["step_minutes"]==0)
        invalid.append(invalid[-1]+int(bool(active) or not working))
    duration, result = request["duration_minutes"], []
    for index in range(max(0,count-duration+1)):
        if eligible[index] and invalid[index+duration]==invalid[index]:
            left = datetime.fromtimestamp(base+index*60, timezone.utc)
            right = left+timedelta(minutes=duration)
            identity = policy.digest({"start":policy.stamp(left),"end":policy.stamp(right)})[:32]
            result.append({"slot_id":identity,"start":policy.stamp(left),"end":policy.stamp(right),
                "local_start":left.astimezone(zone).isoformat(), "local_end":right.astimezone(zone).isoformat(),
                "duration_minutes":duration,"timezone":cfg["timezone"]})
            if len(result)>=cfg["max_slots"]:
                break
    return result
