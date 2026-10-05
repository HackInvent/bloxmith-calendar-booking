"""Explicit calendar events through public runtime, wallet and node storage services."""

import json
import sqlite3
from bloxsmith_app.block_api import BlockDefinition,BlockRuntimeOutput,BlockRuntimePreparation,BlockRuntimeResult
from . import engine,policy,ui


# FB1 - Explicit calendar/participant scope, wallet-only OAuth and fresh source-bound commands.
# FB2 - Real free/busy protocol, working hours, explicit-offset DST slots and buffer constraints.
# FB3 - Local exact drafts, expiration and approval-bound content; previews never invite guests.
# FB4 - Durable one-attempt creation, availability recheck, uncertain outcomes and read-only reconciliation.
# FB5 - Bounded HTTP/storage, cancellation, no redirects/retries and both runtime/package modes.
# FB6 - Owned responsive translated forms, draft Apply/Cancel and exact JSON setting replacement.
class CalendarBookingBlock(BlockDefinition):
    kind="calendar_booking"

    def ui_assets(self,surface="modal"):
        return list(self.model.get("ui_assets",{}).get(surface,[]))

    def prepare_runtime(self,context):
        policy.configuration(context.config)
        return BlockRuntimePreparation()

    def execute_runtime(self,context):
        request_id=None
        try:
            names={int(port.id):port.name for port in context.input_ports}
            if context.input_events:
                events=[(names.get(event.input_port_id),event.value) for event in context.input_events]
            else:
                events=[]
                for ident,name in names.items():
                    attribute=context.input_attribute(name,str(ident))
                    if attribute and attribute.status=="updated" and attribute.value not in (None,""):
                        events.append((name,attribute.value))
            if not events:
                return BlockRuntimeResult(status="skipped",outputs=[])
            if len(events)!=1:
                policy.fail("Send one request or decision per activation.")
            name,value=events[0]
            parsed=policy.document(value)
            request_id=policy.identifier(parsed.get("request_id"))
            values=engine.process(context,name,parsed)
        except (ValueError,TypeError,RuntimeError,OSError,sqlite3.Error) as exc:
            values={"receipt":{"request_id":request_id,"status":"error",
                "code":exc.code if isinstance(exc,policy.CalendarError) else "unavailable",
                "detail":str(exc) if isinstance(exc,policy.CalendarError) else "Calendar operation is unavailable; check configuration and private storage.",
                "uncertain":exc.uncertain if isinstance(exc,policy.CalendarError) else False}}
        outputs=[BlockRuntimeOutput(port_id=int(port.id),port_name=port.name,
            value=json.dumps(values[port.name],ensure_ascii=True,allow_nan=False),content_type="application/json")
            for port in context.output_ports if port.name in values]
        status=values["receipt"]["status"]
        return BlockRuntimeResult(status="success",outputs=outputs,metadata={self.kind:{"status":status}},
            logs=["[calendar-booking] "+status])

    def handle_ui_action(self,*,node,action,values,payload=None):
        if action in {"save_settings","modal_update_fields","inspector_update_fields"}:
            patch=(values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    policy.configuration({**self.default_config(),**(node.get("config") or {}),**patch["config"]})
                except (ValueError,TypeError) as exc:
                    return {"error":self.translate("block.calendar_booking.error",{"detail":str(exc)},fallback="Invalid settings: {detail}")}
        return ui.replace_config(super().handle_ui_action(node=node,
            action="modal_update_fields" if action=="save_settings" else action,values=values,payload=payload),node)

    def render_modal(self,*,node,payload=None):
        return ui.modal(self,node,payload)

    def render_inspector_panel(self,*,node,payload=None):
        return ui.inspector(self,node,payload)

    def render_node_card(self,*,node,payload=None):
        cfg={**self.default_config(),**(node.get("config") or {})}
        key="disabled" if not cfg["enabled"] else "approval" if cfg["booking_enabled"] else "read_only"
        return ui.card(self,node,self.translate("block.calendar_booking."+key,fallback=key))
