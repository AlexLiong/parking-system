"""图像预处理（lpr/preprocess.py）——流水线 ①。

去噪 / 灰度 / CLAHE 增强 / 缩放，抗光照不均（RR-1.1）。所有函数为纯函数，
输入输出均为 ``numpy.ndarray``（BGR 或灰度），便于单测。
"""
from __future__ import annotations

import cv2
import numpy as np


class Preprocessor:
    """预处理组件。"""

    def __init__(self, max_image_side: int = 1280):
        self.max_image_side = max_image_side

    def resize(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        longest = max(h, w)
        if longest > self.max_image_side:
            scale = self.max_image_side / float(longest)
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        return img

    def denoise(self, img: np.ndarray) -> np.ndarray:
        # 双边滤波：保边去噪，适合车牌这类含清晰边缘的图像
        return cv2.bilateralFilter(img, d=7, sigmaColor=60, sigmaSpace=60)

    def to_gray(self, img: np.ndarray) -> np.ndarray:
        if img.ndim == 2:
            return img
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def enhance(self, gray: np.ndarray) -> np.ndarray:
        """CLAHE 自适应直方图均衡，抗光照不均。"""
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        return clahe.apply(gray)

    def process(self, img: np.ndarray) -> np.ndarray:
        """完整预处理：缩放 → 去噪 → 灰度 → CLAHE。返回增强后的灰度图。"""
        img = self.resize(img)
        img = self.denoise(img)
        gray = self.to_gray(img)
        return self.enhance(gray)

    def process_color(self, img: np.ndarray) -> np.ndarray:
        """缩放 + 去噪但保留彩色（供颜色定位/判定使用）。"""
        img = self.resize(img)
        return self.denoise(img)
