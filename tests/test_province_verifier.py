import cv2
import numpy as np

from server.lpr.charset import PROVINCES
from server.lpr.province_verifier import MODEL, suggest, verify


def plate_crop(char, green=False):
    with np.load(MODEL / 'glyphs.npz', allow_pickle=False) as data:
        glyph = data['glyphs'][int(green), PROVINCES.index(char)]
    image = np.full((140, 480 if green else 440, 3),
                    (60, 210, 140) if green else (150, 30, 5), np.uint8)
    block = image[25:115, 15:60]
    block[glyph < 200] = (0, 0, 0) if green else (255, 255, 255)
    return image


def test_reference_province_shape_recovers_both_polarities():
    for green in (False, True):
        crop = plate_crop('藏', green)
        char, score, gap = suggest(crop)
        assert char == '藏' and score > .8 and gap > .15


def test_province_suggestion_and_duplicate_hanzi_preserve_serial():
    crop = plate_crop('青')
    assert verify('津QDTE1H', crop) == ('青QDTE1H', True)
    assert verify('青晋ENW8BX', crop) == ('青ENW8BX', True)
    assert verify('青ENW8BX', crop) == ('青ENW8BX', False)


def test_uninformative_crop_never_invents_a_province():
    assert verify('京A12345', np.full((140, 440, 3), 90, np.uint8)) == ('京A12345', False)
    assert verify(None, None) == (None, False)
