"""Persist externally visible events before notifying WebSocket clients."""
from __future__ import annotations

from ..gateway.ws_realtime import event_to_message
from ..core.events import BarrierCommand, BarrierStateChanged, IncidentRaised
from ..core.timeutil import utcnow
from ..repository.models import BarrierAction, BarrierStateRow


class EventStore:
    def __init__(self, state, broadcaster):
        self.state = state
        self.broadcaster = broadcaster

    def publish(self, event) -> None:
        message = event_to_message(event)
        if message is None:
            return
        db = self.state.db

        def append():
            if isinstance(event, BarrierStateChanged):
                row = db.session.get(BarrierStateRow, event.gate)
                if row is None:
                    row = BarrierStateRow(gate=event.gate, state=event.state,
                                          arm_position=0, pending=0, updated_at=utcnow())
                    db.session.add(row)
                row.state = event.state
                if event.state in ("KEEP_OPEN", "CLOSED"):
                    row.arm_position = 1.0 if event.state == "KEEP_OPEN" else 0.0
                row.keep_open = event.keep_open
                if event.arm_position is not None:
                    row.arm_position = event.arm_position
                if event.state != "FAULT":
                    row.fault = None
                if event.state == "FAULT":
                    for action in db.session.query(BarrierAction).filter_by(gate=event.gate, status="pending"):
                        action.status = "failed"
                row.updated_at = utcnow()
            if isinstance(event, IncidentRaised) and event.kind == "barrier" and event.gate:
                row = db.session.get(BarrierStateRow, event.gate)
                if row is not None:
                    row.fault = str(event.detail.get("reason", "barrier_fault"))
                    row.updated_at = utcnow()
            if isinstance(event, BarrierCommand):
                for action in db.session.query(BarrierAction).filter(
                        BarrierAction.gate == event.gate, BarrierAction.status == "pending",
                        BarrierAction.cmd_id != event.cmd_id):
                    action.status = "interrupted"
                row = db.session.get(BarrierStateRow, event.gate)
                if row is None:
                    row = BarrierStateRow(gate=event.gate, state="CLOSED",
                                          arm_position=0, pending=0, updated_at=utcnow())
                    db.session.add(row)
                row.last_cmd_id = event.cmd_id
                if db.session.query(BarrierAction).filter_by(cmd_id=event.cmd_id).first() is None:
                    db.session.add(BarrierAction(cmd_id=event.cmd_id, gate=event.gate,
                        cmd=event.cmd, reason=event.reason, status="pending",
                        created_at=utcnow()))
            row = self.state.append_event(message["type"], message,
                                          source="system", event_id=event.event_id)
            view = self.state.event_view(row)
            db.on_commit(lambda: self.broadcaster.publish_event(view, message))

        if db.in_scope:
            append()
        else:
            with db.scope():
                append()
