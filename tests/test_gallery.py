def test_gallery_is_authenticated_and_curated(client,admin_token):
    client.cookies.clear()
    assert client.get('/api/recognize/gallery').status_code==401
    response=client.get('/api/recognize/gallery',headers=admin_token)
    assert response.status_code==200,response.text
    samples=response.json()['samples']
    assert len(samples)==18
    assert {x['style'] for x in samples}=={'blue','green_car','green_large','yellow'}
    assert all(set(x)=={'file','plate','color','style','condition'} for x in samples)
    assert client.get('/datasets/demo-lpr/manifest.json',headers=admin_token).status_code==404
