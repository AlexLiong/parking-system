from unittest.mock import Mock
import pytest
from server.core.errors import StateConflict


def test_pause_freezes_backend_timeouts_and_resume(container,monkeypatch):
    clock=container.simulation
    now=[10.]
    monkeypatch.setattr('server.core.simulation.time.monotonic',lambda:now[0])
    clock._last=10
    tick=Mock();container.access.tick=tick
    now[0]=11;clock.tick()
    assert clock.sim_time==1 and tick.call_args.args==(1.,)
    clock.control(paused=True)
    tick.reset_mock();now[0]=16;clock.tick()
    assert clock.sim_time==1;tick.assert_not_called()
    clock.control(paused=False,speed=2)
    now[0]=16.05;clock.tick()
    assert clock.sim_time==pytest.approx(1.1)
    assert tick.call_args.args[0]==pytest.approx(.1)


def test_pause_api_snapshot_and_event(client,admin_token):
    result=client.post('/api/simulation/control',headers=admin_token,json={'paused':True})
    assert result.status_code==200
    snap=client.get('/api/state/snapshot',headers=admin_token).json()
    assert snap['simulation']['paused']
    events=client.get('/api/events',headers=admin_token).json()['items']
    assert any(e['name']=='simulation_state' and e['data']['paused'] for e in events)
    assert client.post('/api/simulation/control',headers=admin_token,json={'speed':12}).status_code==422
    assert client.post('/api/simulation/control',headers=admin_token,json={'speed':0.1}).status_code==422
    ok=client.post('/api/simulation/control',headers=admin_token,json={'speed':8})
    assert ok.status_code==200
    assert ok.json()['speed']==8 and ok.json()['speed_min']==0.25 and ok.json()['speed_max']==10
    assert ok.json()['business_time']
    client.app.state.container.cfg.server.demo_mode=False
    assert client.post('/api/simulation/control',headers=admin_token,json={'paused':False}).status_code==403
