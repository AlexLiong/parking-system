"""Conservative province glyph suggestions using the prescribed MIT assets."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .charset import PROVINCES, PROVINCE_SET, normalize_valid_plate

MODEL = Path(__file__).parent / 'models/reference-provinces'


def normalize(mask):
    ys, xs = np.where(mask > 0)
    if not len(ys):
        return np.zeros((56, 32), np.float32)
    mask = mask[ys.min():ys.max()+1, xs.min():xs.max()+1]
    sample = cv2.resize(mask, (32, 56), interpolation=cv2.INTER_AREA).astype(np.float32)/255
    return (sample-sample.mean())/(sample.std()+1e-6)


@lru_cache(maxsize=1)
def templates():
    manifest = json.loads((MODEL / 'manifest.json').read_text(encoding='utf-8'))
    data = (MODEL / 'glyphs.npz').read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest['sha256'] or manifest['characters'] != PROVINCES:
        raise ValueError('reference glyph integrity mismatch')
    with np.load(MODEL / 'glyphs.npz', allow_pickle=False) as archive:
        glyphs = archive['glyphs']
    return np.array([[normalize((g < 200).astype(np.uint8)*255) for g in group]
                     for group in glyphs])


def suggest(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.countNonZero(cv2.inRange(hsv, (95, 60, 20), (140, 255, 255)))
    green = cv2.countNonZero(cv2.inRange(hsv, (12, 55, 20), (94, 255, 255)))
    is_green = green > blue
    resized = cv2.resize(crop, (480 if is_green else 440, 140))
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)[21:119, 9:66]
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    if is_green:
        mask = 255-mask
    scores = (templates()[int(is_green)] * normalize(mask)).mean(axis=(1, 2))
    order = np.argsort(scores)[::-1]
    return PROVINCES[order[0]], float(scores[order[0]]), float(scores[order[0]]-scores[order[1]])


def verify(plate, crop):
    if not plate or crop is None:
        return plate, False
    char, score, gap = suggest(crop)
    if score < .65 or gap < .15:
        return plate, False
    # CTC sometimes inserts a second leading Chinese character.
    suffix = plate[2:] if len(plate) > 1 and plate[1] in PROVINCE_SET else plate[1:]
    corrected = char+suffix
    try:
        normalize_valid_plate(corrected)
    except ValueError:
        return plate, False
    return corrected, corrected != plate
