"""Loopback-only Google protocol fixture; no real account, token or invitation."""

from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs,unquote,urlsplit

TOKEN="fixture-calendar-access-token"
SECRET=json.dumps({"access_token":TOKEN})
REF="secret://workspace/calendar_fixture"
CALENDAR="organizer@example.test"


def settings():
    return {"enabled":True,"booking_enabled":True,"credential_ref":REF,"calendar_id":CALENDAR,
        "timezone":"UTC","participants":[{"id":"staff","email":"staff@example.test","calendar_id":"staff@example.test"}],
        "business_hours":{day:[["00:00","24:00"]] for day in ("mon","tue","wed","thu","fri","sat","sun")},
        "min_notice_minutes":0}


def request(ident="meeting",**extra):
    start=(datetime.now(timezone.utc)+timedelta(days=2)).replace(hour=0,minute=0,second=0,microsecond=0)
    return {"request_id":ident,"operation":"search","participants":["staff"],
        "period":{"start":start.isoformat(),"end":(start+timedelta(hours=12)).isoformat()},
        "duration_minutes":30,"summary":"Reviewed meeting","description":"Plain <text>",**extra}


@contextmanager
def provider():
    api=SimpleNamespace(calls=[],events={},insertions=[],mode="ok",busy=[],release=threading.Event())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def reply(self,code,body):
            raw=json.dumps(body).encode()
            self.send_response(code);self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)))
            if code==302:
                self.send_header("Location","/must-not-follow")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):
                pass
        def do_POST(self):
            size=int(self.headers.get("Content-Length","0")); assert 0<size<262144
            raw=self.rfile.read(size).decode()
            body=parse_qs(raw) if self.path=="/token" else json.loads(raw)
            api.calls.append({"method":"POST","path":self.path,"body":body,"authorization":self.headers.get("Authorization")})
            if self.path=="/token":
                assert body["grant_type"]==["refresh_token"] and not self.headers.get("Authorization")
                self.reply(200,{"access_token":TOKEN,"token_type":"Bearer","expires_in":3600}); return
            if self.headers.get("Authorization")!="Bearer "+TOKEN:
                self.reply(401,{}); return
            if api.mode=="redirect":
                self.reply(302,{}); return
            if api.mode=="oversized":
                self.reply(200,{"x":"x"*1100000}); return
            if self.path=="/calendar/v3/freeBusy":
                calendars={}
                for item in body["items"]:
                    busy=list(api.busy)
                    for event,calendar in api.events.values():
                        if event["status"]=="confirmed" and (calendar==item["id"] or item["id"] in [p["email"] for p in event.get("attendees",[])]):
                            busy.append({"start":event["start"]["dateTime"],"end":event["end"]["dateTime"]})
                    calendars[item["id"]]={"busy":busy}
                if api.mode=="unavailable":
                    calendars[body["items"][-1]["id"]]={"errors":[{"reason":"notFound"}]}
                self.reply(200,{"kind":"calendar#freeBusy","timeMin":body["timeMin"],"timeMax":body["timeMax"],"calendars":calendars});return
            parts=urlsplit(self.path).path.split("/")
            if len(parts)==6 and parts[3]=="calendars" and parts[5]=="events":
                assert parse_qs(urlsplit(self.path).query)=={"sendUpdates":["all"]}
                if api.mode=="rejected":
                    self.reply(403,{});return
                if body["id"] in api.events:
                    self.reply(409,{});return
                event={**body,"status":"confirmed"}
                api.events[body["id"]]=(event,unquote(parts[4]));api.insertions.append(body)
                if api.mode=="hold":
                    api.release.wait(10)
                if api.mode=="lost":
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.reply(200,event);return
            self.reply(404,{})
        def do_GET(self):
            api.calls.append({"method":"GET","path":self.path,"authorization":self.headers.get("Authorization")})
            ident=urlsplit(self.path).path.rsplit("/",1)[-1]
            if api.mode=="removed":
                self.reply(410,{})
            elif ident not in api.events or api.mode=="missing":
                self.reply(404,{})
            else:
                event=api.events[ident][0]
                self.reply(200,{**event,"summary":"Changed outside the block"} if api.mode=="changed" else event)
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler);server.daemon_threads=True
    api.url="http://127.0.0.1:"+str(server.server_address[1])
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield api
    finally:
        api.release.set();server.shutdown();server.server_close();thread.join(2)


def prepare_http_fixture(directory,api):
    source='''import httpx
_calendar_stream=httpx.AsyncClient.stream
def _calendar_fixture(self,method,url,*args,**kwargs):
    text=str(url)
    if text.startswith("https://www.googleapis.com/calendar/v3/"):
        url=FIXTURE+"/calendar/v3/"+text.split("/calendar/v3/",1)[1]
    elif text=="https://oauth2.googleapis.com/token":
        url=FIXTURE+"/token"
    elif not text.startswith(("http://127.0.0.1:","http://localhost:")):
        raise RuntimeError("External networking is forbidden in Calendar tests")
    return _calendar_stream(self,method,url,*args,**kwargs)
httpx.AsyncClient.stream=_calendar_fixture
'''.replace("FIXTURE",repr(api.url))
    (Path(directory)/"sitecustomize.py").write_text(source,encoding="utf-8")
