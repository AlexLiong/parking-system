import cv2
import numpy as np

from server.lpr.style_refiner import color_crop, select


def test_confident_disagreement_never_automatically_passes():
    plate, _, automatic, candidates = select("湘UF39077", .97,
        [("川UF39077", .99), ("川UF39077", .98), ("川UF39077", .99)], .95)
    assert plate == "川UF39077"
    assert not automatic
    assert "湘UF39077" in candidates


def test_correction_is_suggested_but_requires_manual_confirmation():
    for base in ("陕D184390", "陕D18439D"):
        plate, _, automatic, _ = select(base, .88,
            [("陕D18439D", .97), ("陕D18439D", .98)], .95)
        assert plate == "陕D18439D"
        assert automatic == (base == plate)


def test_crop_does_not_replace_a_stronger_original_or_change_valid_plate_length():
    for extra in ([("津A12345", .90)]*3, [("京A1234", .999)]*3):
        assert select("京A12345", .99, extra, .95)[0] == "京A12345"


def test_single_confident_reading_is_insufficient():
    assert not select("京A12345", .99, [], .95)[2]
    assert not select("京A12345", .99, [("京A12345", .94)], .95)[2]


def test_colored_quad_is_rectified_and_blank_bodywork_is_rejected():
    image = np.full((180, 400, 3), 90, np.uint8)
    assert color_crop(image, [30, 20, 370, 160]) is None
    quad = np.array([[40, 40], [355, 25], [365, 133], [50, 145]], np.int32)
    cv2.fillConvexPoly(image, quad, (180, 40, 10))
    crop = color_crop(image, [30, 20, 375, 155])
    assert crop is not None
    assert 2.7 < crop.shape[1]/crop.shape[0] < 3.2
