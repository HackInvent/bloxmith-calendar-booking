"""Fixed Google HTTPS endpoints, bounded cancellable HTTP and no automatic retries."""

import asyncio
import contextlib
import time
from urllib.parse import quote
from . import policy

CALENDAR_ROOT = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def check(deadline, cancel):
    if cancel():
        policy.fail("Calendar operation cancelled.", "cancelled")
    if time.monotonic()>=deadline:
        policy.fail("Calendar operation deadline exceeded.", "timeout")


class Client:
    def __init__(self, credentials, timeout, cancel=lambda: False):
        self.credentials, self.cancel = credentials, cancel
        self.deadline = time.monotonic()+timeout
        self.token, self.client = None, None

    async def __aenter__(self):
        try:
            import httpx
        except ImportError:
            policy.fail("Install httpx in the runtime interpreter.", "dependency")
        self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(max(.1,self.deadline-time.monotonic()), connect=10),
            headers={"Accept":"application/json","Accept-Encoding":"identity"})
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def request(self, method, url, *, json=None, data=None, authenticated=True):
        import httpx
        check(self.deadline, self.cancel)
        headers = {"Authorization":"Bearer "+self.token} if authenticated else {}
        async def exchange():
            async with self.client.stream(method,url,json=json,data=data,headers=headers) as response:
                if response.headers.get("content-encoding","identity") not in ("identity", ""):
                    policy.fail("Compressed API responses are not accepted.", "protocol")
                content = bytearray()
                async for chunk in response.aiter_raw():
                    content.extend(chunk)
                    if len(content)>1048576:
                        policy.fail("Calendar response exceeded its size limit.", "protocol")
                if response.status_code not in (200,201):
                    return response.status_code, {}
                if "application/json" not in response.headers.get("content-type","").lower():
                    policy.fail("Calendar returned a non-JSON response.", "protocol")
                try:
                    return response.status_code, policy.document(content.decode("utf-8"),1048576)
                except (ValueError,UnicodeError):
                    policy.fail("Calendar returned invalid JSON.", "protocol")
        task = asyncio.create_task(exchange())
        try:
            while not task.done():
                check(self.deadline,self.cancel)
                await asyncio.wait({task},timeout=.03)
            check(self.deadline,self.cancel)
            return task.result()
        except httpx.HTTPError:
            policy.fail("Calendar connection failed; no request was retried.", "network")
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):
                await task

    async def authorize(self):
        if "access_token" in self.credentials:
            self.token = self.credentials["access_token"]
            return
        code, body = await self.request("POST",TOKEN_URL,data={**self.credentials,"grant_type":"refresh_token"},authenticated=False)
        token = body.get("access_token")
        if code!=200 or body.get("token_type","").lower()!="bearer" or not isinstance(token,str) or not 1<=len(token)<=8192 or any(ord(c)<33 or ord(c)>126 for c in token):
            policy.fail("OAuth refresh failed; check the wallet grant and its scopes.", "authentication")
        self.token = token

    async def freebusy(self, ids, start, end):
        code, body = await self.request("POST",CALENDAR_ROOT+"/freeBusy",json={"timeMin":policy.stamp(start),
            "timeMax":policy.stamp(end),"timeZone":"UTC","calendarExpansionMax":50,"items":[{"id":ident} for ident in ids]})
        if code!=200:
            policy.fail("Availability query failed (HTTP "+str(code)+"); no calendar was assumed free.","availability")
        return body

    async def event(self, calendar_id, ident):
        return await self.request("GET",CALENDAR_ROOT+"/calendars/"+quote(calendar_id,safe="")+"/events/"+quote(ident,safe=""))

    async def insert(self, calendar_id, body):
        return await self.request("POST",CALENDAR_ROOT+"/calendars/"+quote(calendar_id,safe="")+"/events?sendUpdates=all",json=body)
