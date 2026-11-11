"""车牌定位（lpr/locator.py）——流水线 ②。

双路候选（传统 CV + 形态学，鲁棒优先，§5.2）：
1. HSV 颜色粗筛：蓝牌 H≈100–124、绿牌 H≈35–90（兼顾渐变绿）生成颜色掩膜；
2. Sobel 垂直边缘 + 形态学闭运算连接字符区；
两路合并后按几何（宽高比、面积占比）与"字符密度/颜色覆盖率"打分排序。
"""
from __future__ import annotations

import cv2
import numpy as np

from ..core.domain import PlateCandidate, PlateColor

# HSV 阈值（OpenCV: H 0-179, S/V 0-255）
_BLUE_LOWER = np.array([100, 60, 40])
_BLUE_UPPER = np.array([124, 255, 255])
_GREEN_LOWER = np.array([35, 40, 40])
_GREEN_UPPER = np.array([90, 255, 255])

_MIN_ASPECT = 2.0
_MAX_ASPECT = 5.5


class PlateLocator:
    def __init__(self, locate_topk: int = 3):
        self.locate_topk = locate_topk

    # ----------------------------- 掩膜 -----------------------------
    def _color_masks(self, hsv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        blue = cv2.inRange(hsv, _BLUE_LOWER, _BLUE_UPPER)
        green = cv2.inRange(hsv, _GREEN_LOWER, _GREEN_UPPER)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 5))
        blue = cv2.morphologyEx(blue, cv2.MORPH_CLOSE, kernel)
        green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, kernel)
        return blue, green

    def _edge_mask(self, gray: np.ndarray) -> np.ndarray:
        sobel = cv2.Sobel(gray, cv2.CV_8U, 1, 0, ksize=3)
        sobel = cv2.convertScaleAbs(sobel)
        _, binary = cv2.threshold(sobel, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 5))
        return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    # ----------------------------- 打分 -----------------------------
    def _color_ratio(self, mask: np.ndarray, x: int, y: int, w: int, h: int) -> float:
        roi = mask[y:y + h, x:x + w]
        if roi.size == 0:
            return 0.0
        return float(np.count_nonzero(roi)) / roi.size

    def _char_density(self, gray: np.ndarray, x: int, y: int, w: int, h: int) -> int:
        """垂直投影峰数（近似字符个数），用于字符密度筛选。"""
        roi = gray[y:y + h, x:x + w]
        if roi.size == 0:
            return 0
        _, bw = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        # 取前景较少的一侧作为字符（车牌字符占比小）
        if np.count_nonzero(bw) > bw.size * 0.5:
            bw = cv2.bitwise_not(bw)
        proj = np.count_nonzero(bw, axis=0)
        thresh = max(2, int(proj.max() * 0.2)) if proj.size else 0
        peaks, in_peak = 0, False
        for v in proj:
            if v > thresh and not in_peak:
                peaks += 1
                in_peak = True
            elif v <= thresh:
                in_peak = False
        return peaks

    def locate(self, img_bgr: np.ndarray, gray: np.ndarray | None = None) -> list[PlateCandidate]:
        """在彩色图上定位车牌，返回按分数降序的候选（最多 locate_topk*2 个）。"""
        h, w = img_bgr.shape[:2]
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        blue_mask, green_mask = self._color_masks(hsv)
        if gray is None:
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        edge_mask = self._edge_mask(gray)
        # Extract each proposal source independently. A noisy edge component
        # connected to the physical plate border must not swallow a valid
        # color rectangle into a frame-sized contour.
        masks = (blue_mask, green_mask, edge_mask)

        img_area = float(h * w)
        candidates: list[PlateCandidate] = []
        contours = []
        for mask in masks:
            found, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours.extend(found)
        for cnt in contours:
            x, y, bw_, bh = cv2.boundingRect(cnt)
            if bw_ < 40 or bh < 15:
                continue
            aspect = bw_ / float(bh)
            if not (_MIN_ASPECT <= aspect <= _MAX_ASPECT):
                continue
            area_ratio = (bw_ * bh) / img_area
            if area_ratio < 0.002 or area_ratio > 0.85:
                continue

            blue_r = self._color_ratio(blue_mask, x, y, bw_, bh)
            green_r = self._color_ratio(green_mask, x, y, bw_, bh)
            density = self._char_density(gray, x, y, bw_, bh)

            color = None
            if blue_r > green_r and blue_r > 0.25:
                color = PlateColor.BLUE
            elif green_r > 0.2 and green_r >= blue_r:
                color = PlateColor.GREEN

            # 打分：颜色覆盖 + 宽高比接近理想(3.14/3.5) + 字符密度
            ideal = 3.5 if color == PlateColor.GREEN else 3.14
            aspect_score = 1.0 - min(1.0, abs(aspect - ideal) / 2.0)
            color_score = max(blue_r, green_r)
            density_score = min(1.0, density / 7.0)
            score = 0.5 * color_score + 0.3 * aspect_score + 0.2 * density_score
            if color is not None:
                score += 0.15  # 命中颜色加权

            angle = self._tilt_angle(gray, x, y, bw_, bh)
            candidates.append(PlateCandidate(
                x=int(x), y=int(y), w=int(bw_), h=int(bh),
                angle=float(angle), score=float(score), color=color))

        candidates.sort(key=lambda c: c.score, reverse=True)
        unique = []
        for candidate in candidates:
            def duplicate(other):
                area=max(0,min(candidate.x+candidate.w,other.x+other.w)-max(candidate.x,other.x))*max(0,min(candidate.y+candidate.h,other.y+other.h)-max(candidate.y,other.y))
                return area/max(1,candidate.w*candidate.h+other.w*other.h-area)>.8
            if not any(duplicate(other) for other in unique):unique.append(candidate)
        return unique[: self.locate_topk * 2]

    def _tilt_angle(self, gray: np.ndarray, x: int, y: int, w: int, h: int) -> float:
        """用最小外接矩形估计倾斜角（度）。"""
        roi = gray[y:y + h, x:x + w]
        if roi.size == 0:
            return 0.0
        _, bw = cv2.threshold(roi, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        coords = cv2.findNonZero(bw)
        if coords is None or len(coords) < 8:
            return 0.0
        rect = cv2.minAreaRect(coords)
        ang = rect[-1]
        # minAreaRect 的角度约定随 OpenCV 版本变化（[0,90] 或 [-90,0)），
        # 水平车牌可能报出 ±90。先归一到 [-45,45)，再把"大角度"视为约定噪声归零
        # （真实拍摄倾斜只有几度，不应出现接近 ±45 以上的旋转）。
        ang = ((ang + 45.0) % 90.0) - 45.0
        if abs(ang) > 20.0:
            ang = 0.0
        return float(ang)

    @staticmethod
    def crop(img: np.ndarray, cand: PlateCandidate, pad: int = 4) -> np.ndarray:
        h, w = img.shape[:2]
        x0 = max(0, cand.x - pad)
        y0 = max(0, cand.y - pad)
        x1 = min(w, cand.x + cand.w + pad)
        y1 = min(h, cand.y + cand.h + pad)
        return img[y0:y1, x0:x1]
