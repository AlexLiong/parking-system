"""Color-boundary rectification and photometric agreement for single plates.

No labels, benchmark metadata, or external generator files are read at inference.
The color crop is deliberately restricted to an already detected plate box.
"""
from __future__ import annotations

from collections import defaultdict

import cv2
import numpy as np

from .charset import normalize_valid_plate


def color_crop(image: np.ndarray, bbox) -> np.ndarray | None:
    h, w = image.shape[:2]
    x0, y0, x1, y1 = map(int, bbox)
    margin = max(3, int((y1-y0) * .1))
    x0, y0 = max(0, x0-margin), max(0, y0-margin)
    x1, y1 = min(w, x1+margin), min(h, y1+margin)
    roi = image[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # Blue, green, and the yellow/green large NE layout; exclude gray bodywork.
    mask = cv2.inRange(hsv, (12, 55, 20), (140, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    quad = cv2.approxPolyDP(contour, .025 * cv2.arcLength(contour, True), True)
    if (len(quad) != 4 or not cv2.isContourConvex(quad)
            or cv2.contourArea(quad) < .35 * roi.shape[0] * roi.shape[1]):
        return None
    points = quad.reshape(4, 2).astype(np.float32)
    sums, differences = points.sum(1), points[:, 0]-points[:, 1]
    points = points[[np.argmin(sums), np.argmax(differences),
                     np.argmax(sums), np.argmin(differences)]]
    width = max(np.linalg.norm(points[1]-points[0]), np.linalg.norm(points[2]-points[3]))
    height = max(np.linalg.norm(points[3]-points[0]), np.linalg.norm(points[2]-points[1]))
    if height < 15 or not 2 < width/height < 6:
        return None
    destination = np.float32([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]])
    transform = cv2.getPerspectiveTransform(points, destination)
    return cv2.warpPerspective(roi, transform, (int(width), int(height)),
                               flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def readings(image: np.ndarray, bbox, recognizer) -> list[tuple[str, float]]:
    crop = color_crop(image, bbox)
    if crop is None:
        return []
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = cv2.createCLAHE(2., (4, 4)).apply(lab[:, :, 0])
    variants = [crop, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
                cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)]
    return [(str(plate).replace("·", "").strip(), float(conf))
            for plate, conf in (recognizer(variant) for variant in variants)]


def select(base: str, confidence: float, extra: list[tuple[str, float]],
           threshold: float) -> tuple[str, float, bool, list[str]]:
    """Select by repeated valid readings; high confidence disagreement blocks auto.

    Augmented crops are correlated observations, so this is an agreement guard,
    not a claim that their scores are calibrated independent probabilities.
    """
    pool: dict[str, list[float]] = defaultdict(list)
    all_readings = [(base, confidence), *extra]
    for plate, conf in all_readings:
        try:
            normalize_valid_plate(plate)
        except (ValueError, AttributeError):
            continue
        if np.isfinite(conf):
            pool[plate].append(conf)
    stable = {plate: scores for plate, scores in pool.items() if len(scores) >= 2}
    if base in pool:
        stable = {p: scores for p, scores in stable.items() if len(p) == len(base)
                  and sum(scores)/len(scores) > confidence}
    plate = max(stable, key=lambda p: sum(stable[p])/len(stable[p])) if stable else base
    scores = pool.get(plate, [])
    agreement = (sum(score >= threshold for score in scores) >= 2
                 and plate == base
                 and not any(p != plate and c >= threshold for p, c in all_readings))
    confidence = sum(scores)/len(scores) if scores else confidence
    return plate, confidence, agreement, list(dict.fromkeys([plate, *pool]))
