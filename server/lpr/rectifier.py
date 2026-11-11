"""车牌校正与颜色判定（lpr/rectifier.py）——流水线 ③④。

对定位候选区做倾斜旋转校正并缩放到标准尺寸（蓝牌 440×140 / 绿牌 480×140）；
再统计 HSV 主色判定蓝/绿，决定字符数（7/8）与校验规则。
"""
from __future__ import annotations

import cv2
import numpy as np

from ..core.domain import PlateCandidate, PlateColor


class PlateRectifier:
    def __init__(self, blue_size=(440, 140), green_size=(480, 140)):
        self.blue_size = tuple(blue_size)
        self.green_size = tuple(green_size)

    def rotate(self, img: np.ndarray, angle: float) -> np.ndarray:
        if abs(angle) < 0.5:
            return img
        h, w = img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)

    def rectify(self, plate_img: np.ndarray, cand: PlateCandidate | None = None,
                target_size: tuple[int, int] | None = None) -> np.ndarray:
        """旋转校正 + 缩放到标准尺寸。target_size 为 (w, h)。"""
        if cand is not None and cand.angle:
            plate_img = self.rotate(plate_img, cand.angle)
        if target_size is None:
            target_size = self.blue_size
        w, h = int(target_size[0]), int(target_size[1])
        return cv2.resize(plate_img, (w, h), interpolation=cv2.INTER_CUBIC)

    def detect_color(self, plate_bgr: np.ndarray) -> PlateColor:
        """统计车牌条 HSV 主色判定蓝/绿。"""
        hsv = cv2.cvtColor(plate_bgr, cv2.COLOR_BGR2HSV)
        h = hsv[:, :, 0]
        s = hsv[:, :, 1]
        # 只统计有一定饱和度的像素（排除白字符/黑字符）
        sat_mask = s > 50
        if np.count_nonzero(sat_mask) < 20:
            return PlateColor.BLUE
        hues = h[sat_mask]
        blue = np.count_nonzero((hues >= 100) & (hues <= 124))
        green = np.count_nonzero((hues >= 35) & (hues <= 90))
        return PlateColor.GREEN if green > blue else PlateColor.BLUE

    def target_size_for(self, color: PlateColor | str | None) -> tuple[int, int]:
        return self.green_size if color in (PlateColor.GREEN, "green") else self.blue_size
