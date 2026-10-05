"""FB1/FB2/FB3/FB4/FB5: real HTTP fixture, wallet, runtime Stop and package-origin persistence."""

import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"tests"),str(Path(__file__).parent)]
from blocs.calendar_booking.block import CalendarBookingBlock
from block_test_packages import install_test_package,prepare_release_run,surface_payload
from calendar_fixture import provider,prepare_http_fixture,settings,request,SECRET,TOKEN
from ui_smoke_common import (isolated_server,http_json,graph_payload,text_node,data_edge,create_project_api,
    graph_storage_dir,create_run_api,wait_for_run_terminal,wait_for_run_predicate,stop_run_api)


def value(data,port):
    raw=data.get("output_values",{}).get("agenda:"+str(port),{}).get("value")
    return json.loads(raw) if raw else {}


def decision(draft):
    return {**{key:draft[key] for key in ("request_id","draft_id","revision")},"decision":"approve"}


def main():
    for mode in ("centralized","zeromq_active"):
        for origin in (None,"managed","linked"):
            with provider() as api,isolated_server(prepare=lambda root:prepare_http_fixture(root,api)) as server:
                node=CalendarBookingBlock().build_node_payload(node_id="agenda",position={"x":430,"y":160},config_overrides=settings())
                node["inputs"].reverse();node["outputs"].reverse()
                if origin:
                    model=install_test_package(server,"calendar_booking",origin=origin)
                    node["block_version"]=model["version"]
                    for surface in ("modal","inspector_panel","node_card"):
                        rendered=surface_payload(server,model,node,surface)["html"]
                        assert "{{" not in rendered and TOKEN not in rendered
                http_json(server.base_url,"/api/application/secrets/init",method="POST",payload={"password":"temporary-calendar-test"})
                http_json(server.base_url,"/api/application/secrets",method="POST",payload={"name":"calendar_fixture","value":SECRET})
                graph=graph_payload("Explicit calendar decisions",[text_node("request","Request","",60,100),
                    text_node("decision","Approval","",60,300),node],[data_edge("request","request",1,"agenda",1),
                    data_edge("decision","decision",1,"agenda",2)])
                project=create_project_api(server,document=graph)["project"]
                storage=graph_storage_dir(server,project)/"instances"/"1"/"blocs"/"agenda"
                if mode=="centralized":
                    def command(source,obj):
                        for item in graph["nodes"][:2]:
                            item["outputs"][0]["text"]=json.dumps(obj) if item["id"]==source else ""
                        (graph_storage_dir(server,project)/"graph.json").write_text(json.dumps({**graph,"graph_id":project["graph_id"]}),encoding="utf-8")
                        http_json(server.base_url,f"/api/projects/{project['graph_id']}/graph/reload",method="POST",payload={})
                        run=create_run_api(server,graph,project_id=project["graph_id"],runtime_mode=mode)
                        data=wait_for_run_terminal(server,run["run_id"],timeout_sec=20)
                        assert data["status"]=="success",data.get("logs")
                        assert TOKEN not in json.dumps(data)
                        return data
                    proposed=value(command("request",request()),1)
                    assert proposed.get("options"),proposed
                    drafted=value(command("request",{"request_id":"meeting","operation":"draft","slot_id":proposed["options"][0]["slot_id"]}),2)
                    assert drafted and not api.insertions
                    confirmed=value(command("decision",decision(drafted)),3)
                    assert confirmed["status"]=="confirmed" and len(api.insertions)==1,confirmed
                    repeated=value(command("decision",decision(drafted)),3)
                    assert repeated["duplicate"] and len(api.insertions)==1,repeated
                else:
                    def start():
                        rid=prepare_release_run(server,project["graph_id"],graph)["run_id"]
                        http_json(server.base_url,f"/api/runs/{rid}/play",method="POST",payload={})
                        return rid
                    rid=start()
                    assert not api.calls,"Preparation or Play contacted Google without an input"
                    def send(source,obj):
                        http_json(server.base_url,f"/api/runs/{rid}/active/control",method="POST",payload={"action":"publish_output",
                            "node_id":source,"port_id":1,"value":json.dumps(obj),"content_type":"application/json"})
                    def wait(predicate):
                        data=wait_for_run_predicate(server,rid,lambda item:item.get("status")=="failed" or predicate(item),
                            "Calendar command did not complete",timeout_sec=20)
                        assert data["status"]!="failed",data.get("logs")
                        assert TOKEN not in json.dumps(data)
                        return data
                    def prepare(ident):
                        send("request",request(ident))
                        proposed=value(wait(lambda item:value(item,1).get("request_id")==ident),1)
                        assert proposed["options"],proposed
                        send("request",{"request_id":ident,"operation":"draft","slot_id":proposed["options"][0]["slot_id"]})
                        return value(wait(lambda item:value(item,2).get("request_id")==ident),2)
                    def stop():
                        started=time.monotonic()
                        stop_run_api(server,rid)
                        stopped=wait_for_run_terminal(server,rid,timeout_sec=15)
                        assert time.monotonic()-started<8,stopped.get("logs")
                        assert "shutdown_timeout" not in str(stopped.get("logs"))
                    try:
                        drafted=prepare("meeting")
                        assert not api.insertions
                        send("decision",{**decision(drafted),"revision":"0"*64})
                        wait(lambda item:value(item,3).get("code")=="conflict")
                        assert not api.insertions
                        send("decision",decision(drafted))
                        confirmed=value(wait(lambda item:value(item,3).get("status")=="confirmed"),3)
                        assert confirmed["event_id"]==api.insertions[0]["id"] and len(api.insertions)==1
                        stop();rid=start()
                        send("decision",decision(drafted))
                        wait(lambda item:value(item,3).get("duplicate"))
                        assert len(api.insertions)==1
                        # The provider accepts an event then holds its response. Real Stop may kill
                        # the package host before finally runs; the pre-POST record must still survive.
                        waiting=prepare("stop-during-booking")
                        api.release.clear();api.mode="hold"
                        send("decision",decision(waiting))
                        deadline=time.monotonic()+8
                        while len(api.insertions)<2 and time.monotonic()<deadline:
                            time.sleep(.02)
                        assert len(api.insertions)==2,http_json(server.base_url,f"/api/runs/{rid}")
                        stop();api.mode="ok";api.release.set();rid=start()
                        send("decision",decision(waiting))
                        persisted=value(wait(lambda item:value(item,3).get("request_id")=="stop-during-booking"),3)
                        assert persisted["status"]=="uncertain" and persisted["duplicate"],persisted
                        assert len(api.insertions)==2
                        send("request",{"request_id":"stop-during-booking","operation":"reconcile"})
                        recovered=value(wait(lambda item:value(item,3).get("reconciled")),3)
                        assert recovered["status"]=="confirmed" and len(api.insertions)==2
                        http_json(server.base_url,"/api/application/secrets/lock",method="POST",payload={})
                        before=len(api.calls)
                        send("request",request("wallet-locked"))
                        wait(lambda item:value(item,3).get("code")=="secret")
                        assert len(api.calls)==before
                    finally:
                        api.release.set();stop()
                assert (storage/"calendar.sqlite3").is_file()
                assert TOKEN.encode() not in (storage/"calendar.sqlite3").read_bytes()
            print("[ok] Calendar HTTP + wallet + exact approval + persistence "+mode+" "+str(origin),flush=True)


if __name__=="__main__":
    main()
