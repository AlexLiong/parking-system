"""Package the cited MIT province glyphs for local inference (no system font)."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.lpr.charset import PROVINCES
from tools.generate_reference_plates import SOURCE, SOURCE_COMMIT


def build():
    assert subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'],
                                   text=True).strip() == SOURCE_COMMIT
    assert not subprocess.check_output(['git', '-C', str(SOURCE), 'diff', '--name-only',
                                        'HEAD', '--', 'font_model', 'LICENSE'], text=True).strip()
    output = ROOT / 'server/lpr/models/reference-provinces'
    output.mkdir(parents=True, exist_ok=True)
    glyphs, sources = [], []
    for style in ('140', 'green'):
        group = []
        for char in PROVINCES:
            source = SOURCE / 'font_model' / f'{style}_{char}.jpg'
            data = source.read_bytes()
            glyph = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            group.append(cv2.resize(glyph, (45, 90)))
            sources.append({'file': source.name, 'sha256': hashlib.sha256(data).hexdigest()})
        glyphs.append(group)
    np.savez_compressed(output / 'glyphs.npz', glyphs=np.asarray(glyphs, np.uint8))
    manifest = {'source_commit': SOURCE_COMMIT, 'source': str(SOURCE.name),
                'characters': PROVINCES, 'styles': ['140', 'green'], 'sources': sources,
                'sha256': hashlib.sha256((output / 'glyphs.npz').read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    (output / 'LICENSE').write_bytes((SOURCE / 'LICENSE').read_bytes())
    print(json.dumps({'glyphs': 62, 'sha256': manifest['sha256']}))


if __name__ == '__main__':
    build()
