from server.core.timeutil import utcnow

def seed(client,plate='沪UD76840',slot='A-01',photo='datasets/demo-lpr/green_car-000-clean.png',status='in'):
 c=client.app.state.container
 with c.db.scope():
  r=c.parking_repo.create(plate=plate,plate_color='green',entry_time=utcnow(),space_no=slot,entry_photo=photo,status=status)
  if slot:c.space_repo.occupy(slot,plate)
 return r.id

def test_find_car_requires_auth_and_exact_current_plate(client,admin_token):
 seed(client)
 client.cookies.clear()  # admin_token fixture logged in using a session cookie
 assert client.get('/api/find-car?plate=沪UD76840').status_code==401
 r=client.get('/api/find-car',params={'plate':'沪u·d76840'},headers=admin_token)
 assert r.status_code==200,r.text
 data=r.json();assert data['plate']=='沪UD76840' and data['located'] and data['space_no']=='A-01'
 assert data['entry_photo'].endswith('green_car-000-clean.png') and data['location']['zone']=='A'
 assert client.get('/api/find-car?plate=沪UD76841',headers=admin_token).status_code==404
 assert client.get('/api/find-car?plate=foo',headers=admin_token).status_code==422

def test_find_car_historical_and_no_slot(client,admin_token):
 seed(client,plate='京A12345',slot=None,photo=None,status='out')
 assert client.get('/api/find-car?plate=京A12345',headers=admin_token).status_code==404
 seed(client,slot=None,photo=None)
 r=client.get('/api/find-car?plate=沪UD76840',headers=admin_token).json()
 assert r['located'] is False and r['location'] is None and not r['navigation'] and r['entry_photo'] is None

def test_find_car_refuses_inconsistent_occupancy(client,admin_token):
 seed(client);c=client.app.state.container
 with c.db.scope():c.space_repo.release('A-01')
 assert client.get('/api/find-car?plate=沪UD76840',headers=admin_token).status_code==409

def test_guidance_uses_real_zone_coordinates(client,admin_token):
 for plate,slot,direction in [('京A10001','A-11','北侧 A区西侧'),('京A10002','C-01','北侧 C区东侧'),('京A10003','C-11','北侧 C区东侧'),('京A10004','B-01','南侧 B区西侧'),('京A10005','D-11','南侧 D区东侧')]:
  seed(client,plate=plate,slot=slot)
  result=client.get('/api/find-car',params={'plate':plate},headers=admin_token).json()
  assert result['navigation'][1]=='前往'+direction
