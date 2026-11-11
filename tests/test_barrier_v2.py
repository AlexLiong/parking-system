from server.access.barrier import BarrierController
from server.core.config import AccessConfig
from server.core.events import BarrierCommand, BarrierStateChanged, EventBus


def test_command_precedes_transient_and_final_state_retains_id():
    bus = EventBus(); events = []; bus.subscribe_all(events.append)
    b = BarrierController('in', AccessConfig(), bus)
    b.request_open('entry')
    assert isinstance(events[0], BarrierCommand)
    assert isinstance(events[1], BarrierStateChanged) and events[1].transient
    command = events[0].cmd_id
    assert not b.on_ack('open', 'old', require_cmd=True)
    assert b.on_ack('open', command, require_cmd=True)
    assert events[-1].cmd_id == command and not events[-1].transient
    assert not b.on_ack('open', command, require_cmd=True)


def test_stop_invalidates_motion_ack_and_keep_open_can_be_released():
    b = BarrierController('in', AccessConfig(), EventBus())
    b.request_open(); old = b._ack_cmd_id
    b.request_stop(); stop = b._ack_cmd_id
    assert b.state.value == 'STOPPED'
    assert not b.on_ack('open', old, require_cmd=True)
    assert b.on_ack('stopped', stop, require_cmd=True)
    b.tick(100); assert b.state.value == 'STOPPED'
    b.set_keep_open(True); b.on_ack('open', b._ack_cmd_id)
    b.tick(100); assert b.state.value == 'KEEP_OPEN'
    b.set_keep_open(False); b.tick(100)
    assert b.state.value == 'CLOSING'


def test_manual_api_position_audit_and_export(client, admin_token):
    c = client.app.state.container
    opened = client.post('/api/manual/open-barrier', headers=admin_token,
                         json={'gate': 'in'}).json()
    assert c.access._pending['in'] == 0
    assert client.post('/sim/events/barrier_ack', json={'gate':'in', 'state':'open'}).json()['accepted'] is False
    assert client.post('/sim/events/barrier_ack', json={'gate':'in', 'state':'open',
                                                      'cmd_id':opened['cmd_id']}).json()['accepted']
    c.access.barriers['in'].on_zone_occupied(True)
    assert client.post('/api/manual/close-barrier', headers=admin_token,
                       json={'gate':'in'}).status_code == 409
    c.access.barriers['in'].on_zone_occupied(False)
    client.post('/api/manual/close-barrier', headers=admin_token, json={'gate':'in'})
    stopped = client.post('/api/manual/stop-barrier', headers=admin_token, json={'gate':'in'}).json()
    assert client.post('/sim/events/barrier_ack', json={'gate':'in', 'state':'stopped',
        'cmd_id':stopped['cmd_id'], 'arm_position':.43}).json()['accepted']
    snapshot = client.get('/api/state/snapshot', headers=admin_token).json()['barriers']['in']
    assert snapshot['state'] == 'STOPPED' and snapshot['arm_position'] == .43
    actions = client.get('/api/barrier/actions/export', headers=admin_token)
    assert actions.status_code == 200
    stop = next(a for a in actions.json()['items'] if a['cmd_id']==stopped['cmd_id'])
    assert stop['operator']=='admin' and stop['status']=='acked'


def test_fault_injection_timeout_and_recovery_resolves_incident(client, admin_token):
    c = client.app.state.container
    for mode in ('stuck_open', 'stuck_closed', 'ack_timeout'):
        response = client.post('/api/barrier/in/fault', headers=admin_token,
                               json={'mode':mode})
        assert response.status_code==200, response.text
        b = c.access.barriers['in']
        if mode == 'ack_timeout':
            assert not b.on_ack('open', b._ack_cmd_id)
            for _ in range(c.cfg.access.barrier_retry+1):
                b.tick(c.cfg.access.barrier_ack_timeout_sec+.1)
        assert b.state.value=='FAULT'
        # 实现 A：FAULT 下允许人工抬杆（手动曲柄）；物理可通行 ≠ 业务授权
        opened = client.post('/api/manual/open-barrier', headers=admin_token,
                             json={'gate':'in'})
        assert opened.status_code==200, opened.text
        body = opened.json()
        assert body['manual'] is True and body['authorization']=='manual'
        assert body['passable'] is True
        assert client.post('/sim/events/barrier_ack', json={
            'gate':'in', 'state':'open', 'cmd_id':body['cmd_id']}).json()['accepted']
        assert b.state.value=='KEEP_OPEN' and b.is_passable()
        response = client.post('/api/barrier/in/clear-fault', headers=admin_token)
        assert response.status_code==200, response.text
        closed = client.post('/api/manual/close-barrier', headers=admin_token,
                             json={'gate':'in'}).json()
        assert client.post('/sim/events/barrier_ack', json={
            'gate':'in', 'state':'closed', 'cmd_id':closed['cmd_id']}).json()['accepted']
        assert b.state.value=='CLOSED'
        assert not c.incident_repo.open_by_kind('barrier','in')


def test_keep_open_is_durable_and_actions_require_authorization(client, admin_token):
    client.cookies.clear()
    assert client.post('/api/barrier/in/keep-open', json={'enabled':True}).status_code==401
    result = client.post('/api/barrier/in/keep-open', headers=admin_token, json={'enabled':True})
    assert result.status_code==200
    b = client.app.state.container.access.barriers['in']
    client.post('/sim/events/barrier_ack', headers=admin_token, json={'gate':'in','state':'open', 'cmd_id':result.json()['cmd_id']})
    client.app.state.container.access.restore_barriers()
    assert b.keep_open and b.state.value=='KEEP_OPEN'
    client.post('/api/barrier/in/keep-open', headers=admin_token, json={'enabled':False})
    assert not b.keep_open
