"""Package exact MIT reference glyphs/bases; no system font and no 3D asset changes."""
import hashlib,json,shutil,subprocess,sys
from pathlib import Path
import cv2,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server.lpr.charset import PROVINCES,ALNUM
from tools.generate_reference_plates import SOURCE,SOURCE_COMMIT

def build():
    assert subprocess.check_output(['git','-C',str(SOURCE),'rev-parse','HEAD'],text=True).strip()==SOURCE_COMMIT
    assert not subprocess.check_output(['git','-C',str(SOURCE),'diff','--name-only','HEAD','--','font_model','plate_model','LICENSE'],text=True).strip()
    out=ROOT/'server/lpr/models/reference-plates';out.mkdir(exist_ok=True)
    chars=list(PROVINCES)+list(ALNUM);arrays={};sources=[]
    for style in ['140','green']:
        group=[]
        for ch in chars:
            p=SOURCE/'font_model'/f'{style}_{ch}.jpg';data=p.read_bytes()
            group.append(cv2.resize(cv2.imdecode(np.frombuffer(data,np.uint8),0),(45,90)))
            sources.append({'path':f'font_model/{p.name}','sha256':hashlib.sha256(data).hexdigest()})
        arrays['glyphs_'+style]=np.asarray(group,np.uint8)
    for style in ['blue','green_car','green_truck']:
        p=SOURCE/'plate_model'/f'{style}_140.PNG';data=p.read_bytes();arrays['base_'+style]=cv2.imdecode(np.frombuffer(data,np.uint8),1)
        sources.append({'path':f'plate_model/{p.name}','sha256':hashlib.sha256(data).hexdigest()})
    np.savez_compressed(out/'assets.npz',**arrays)
    manifest={'source':'https://github.com/Pengfei8324/chinese_license_plate_generator','source_commit':SOURCE_COMMIT,'license':'MIT; Copyright (c) 2020 leijd','characters':chars,'sources':sources,'sha256':hashlib.sha256((out/'assets.npz').read_bytes()).hexdigest()}
    (out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');shutil.copy2(SOURCE/'LICENSE',out/'LICENSE')
    print({'glyphs':len(chars)*2,'source_commit':SOURCE_COMMIT})
if __name__=='__main__':build()
