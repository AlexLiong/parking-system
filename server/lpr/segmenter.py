"""字符分割（lpr/segmenter.py）——流水线 ⑤。

自适应二值化（Otsu，含极性判定）→ 连通域提取 → 规则过滤（丢弃间隔符·与噪声）
→ 与期望字符数（蓝 7 / 绿 8）对齐（合并过分割 / 拆分粘连）→ 先验等宽窗格兜底
→ 输出归一化字符块（char_size，白字黑底灰度）。
"""
from __future__ import annotations

import cv2
import numpy as np

from ..core.domain import PlateColor


class CharSegmenter:
    def __init__(self, char_size=(20, 40)):
        self.char_w, self.char_h = int(char_size[0]), int(char_size[1])

    # ----------------------------- 二值化 -----------------------------
    def binarize(self, plate_gray: np.ndarray) -> np.ndarray:
        """Otsu 二值化并判定极性，使字符为白（255）、背景为黑（0）。"""
        blur = cv2.medianBlur(plate_gray, 3)
        _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        # 字符约占车牌 12%~35%；据此选择极性
        ratio = np.count_nonzero(bw) / bw.size
        if ratio > 0.5:
            bw = cv2.bitwise_not(bw)
            ratio = 1 - ratio
        if ratio < 0.05 or ratio > 0.6:
            # 兜底：局部自适应阈值
            bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                       cv2.THRESH_BINARY, 25, -8)
            if np.count_nonzero(bw) / bw.size > 0.5:
                bw = cv2.bitwise_not(bw)
        return bw

    def _clear_border(self, bw: np.ndarray) -> np.ndarray:
        """清除与图像边界连通的白色区域（背景环）。

        定位裁剪会带入车牌外的场景背景；对绿牌（黑字）二值化反相后，
        较暗的场景背景会变成白色前景并贴着边界，干扰连通域。字符位于牌内、
        不贴边界，故可安全移除贴边白色连通块。
        """
        h, w = bw.shape
        ff = bw.copy()
        mask = np.zeros((h + 2, w + 2), np.uint8)
        step_x = max(1, w // 8)
        step_y = max(1, h // 8)
        seeds = [(x, 0) for x in range(0, w, step_x)] + [(x, h - 1) for x in range(0, w, step_x)]
        seeds += [(0, y) for y in range(0, h, step_y)] + [(w - 1, y) for y in range(0, h, step_y)]
        for (x, y) in seeds:
            if ff[y, x] == 255:
                cv2.floodFill(ff, mask, (x, y), 0)
        # The physical white frame may be inset from the crop boundary. It is
        # a thin component spanning almost the entire plate, not a glyph.
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ff, connectivity=8)
        for i in range(1,count):
            cx,cy,cw,ch,area=stats[i]
            if (cw>w*.8 and ch>h*.75 and area<cw*ch*.25) or (ch<h*.2 and (cy<h*.16 or cy+ch>h*.84)):
                ff[labels==i]=0
        return ff

    def _content_bbox(self, bw: np.ndarray) -> tuple[int, int, int, int]:
        cols = np.count_nonzero(bw, axis=0)
        rows = np.count_nonzero(bw, axis=1)
        xs = np.where(cols > 0)[0]
        ys = np.where(rows > 0)[0]
        if len(xs) == 0 or len(ys) == 0:
            return 0, 0, bw.shape[1], bw.shape[0]
        return int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1

    # ----------------------------- 分割 -----------------------------
    def segment(self, plate_img: np.ndarray, n_expected: int,
                color: PlateColor | str | None = None) -> list[np.ndarray]:
        """返回 n_expected 个归一化字符块（灰度，白字黑底）。"""
        gray = plate_img if plate_img.ndim == 2 else cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
        bw = self._plate_binary(gray, n_expected)
        H, W = bw.shape
        x0, y0, x1, y1 = self._content_bbox(bw)
        bw_c = bw[y0:y1, x0:x1]
        ch, cw = bw_c.shape
        if ch < 4 or cw < 8:
            return self._grid_fallback(gray, n_expected)

        boxes = self._component_boxes(bw_c, ch, cw)
        boxes = self._align_count(boxes, n_expected, ch, cw)
        if len(boxes) != n_expected:
            return self._grid_fallback(gray, n_expected)

        chars = []
        for (bx, by, bww, bh) in boxes:
            sub = bw_c[by:by + bh, bx:bx + bww]
            # bw_c 已由 binarize() 依据整牌字符占比先验统一为“白字黑底”，
            # 连通域紧裁剪的边界环就是字形本身，若在此再判极性会因笔画粗细
            # 误反相（P/D/E/A/3 等触边字符尤甚），故主路径信任整牌极性。
            chars.append(self._normalize(sub, detect_polarity=False))
        return chars

    def _component_boxes(self, bw: np.ndarray, ch: int, cw: int) -> list[tuple[int, int, int, int]]:
        """连通域 → 过滤间隔符/噪声 → 字符框列表（按 x 排序）。"""
        n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
        boxes = []
        for i in range(1, n):
            x,y,w,h,area=stats[i]
            if w>=1 and h>=2 and area>=4:boxes.append((x,y,w,h))
        boxes.sort(key=lambda b:b[0])
        # Disconnected Chinese radicals must join before height/area filtering.
        merged=self._merge_close(boxes, gap_limit=ch*.1)
        return [b for b in merged if b[3]>=ch*.45 and b[2]>=max(2,cw*.02) and b[2]*b[3]>=ch*cw*.004]

    def _merge_close(self, boxes: list[tuple[int, int, int, int]], gap_limit=0) -> list[tuple[int, int, int, int]]:
        """合并 x 方向重叠/极近的框（同一字符被拆成多块，如断裂笔画）。"""
        if not boxes:
            return boxes
        merged = [list(boxes[0])]
        for b in boxes[1:]:
            last = merged[-1]
            gap = b[0] - (last[0] + last[2])
            # 重叠或间隙很小 → 合并
            if gap < max(2, int(last[2] * 0.15), gap_limit):
                nx0 = min(last[0], b[0])
                ny0 = min(last[1], b[1])
                nx1 = max(last[0] + last[2], b[0] + b[2])
                ny1 = max(last[1] + last[3], b[1] + b[3])
                merged[-1] = [nx0, ny0, nx1 - nx0, ny1 - ny0]
            else:
                merged.append(list(b))
        return [tuple(m) for m in merged]

    def _align_count(self, boxes: list[tuple[int, int, int, int]], n: int,
                     ch: int, cw: int) -> list[tuple[int, int, int, int]]:
        """把框数量对齐到 n：过宽框按期望宽度拆分。"""
        if len(boxes) == n:
            return boxes
        if len(boxes) > n:
            # 优先按面积保留最大的 n 个（噪声残留情形）
            return sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)[:n] and \
                sorted(sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)[:n], key=lambda b: b[0])
        # 过少：拆分最宽的框
        est_w = cw / float(n + 1)
        result: list[tuple[int, int, int, int]] = []
        for (x, y, w, h) in boxes:
            parts = max(1, int(round(w / est_w))) if w > est_w * 1.5 else 1
            if parts <= 1:
                result.append((x, y, w, h))
            else:
                sub_w = w / parts
                for k in range(parts):
                    result.append((int(x + k * sub_w), y, int(sub_w), h))
        result.sort(key=lambda b: b[0])
        return result

    def _plate_binary(self, gray, n):
        bw=self._clear_border(self.binarize(gray))
        h,w=bw.shape
        if n>1 and w/h>2:
            # Standard single-row Chinese plates put characters inside this
            # band. Exclude inset frames/rivets before measuring glyph bounds.
            bw[:int(h*.15)]=0;bw[int(h*.85):]=0
            bw[:,:int(w*.025)]=0;bw[:,int(w*.975):]=0
        return bw

    def _grid_fallback(self, gray: np.ndarray, n: int) -> list[np.ndarray]:
        """先验等宽窗格兜底（污损/二值化失败时，宁缺毋滥交给分类器+仲裁）。"""
        bw = self._plate_binary(gray,n)
        x0, y0, x1, y1 = self._content_bbox(bw)
        inner = bw[y0:y1, x0:x1]
        if inner.size == 0:
            inner = bw
        H, W = inner.shape
        # 车牌左右留白约 3%，均分 n 格
        left = int(W * 0.03)
        right = int(W * 0.97)
        span = max(1, right - left)
        cell = span / float(n)
        chars = []
        for i in range(n):
            cx0 = int(left + i * cell)
            cx1 = int(left + (i + 1) * cell)
            sub = inner[:, cx0:cx1]
            chars.append(self._normalize(sub, detect_polarity=False))
        return chars

    def _normalize(self, char_bin: np.ndarray, *, detect_polarity: bool = True) -> np.ndarray:
        """裁剪到内容并缩放到 char_size，返回白字黑底灰度。

        ``detect_polarity``：是否用“边界环”重新判定极性。仅在输入为**未二值化的
        灰度块**（如 ``_grid_fallback`` 的等宽窗格）时需要；主分割路径的块已是
        白字黑底的二值图，必须传 ``False``，否则紧裁剪边界环恰为字形会误反相。
        """
        if char_bin.size == 0:
            return np.zeros((self.char_h, self.char_w), dtype=np.uint8)
        if char_bin.ndim == 3:
            char_bin = cv2.cvtColor(char_bin, cv2.COLOR_BGR2GRAY)
        if detect_polarity:
            # 确保白字黑底：以"边界环"（背景）判定极性。
            # 不能用整块白占比：紧包围盒内笔画密集的字符（A/8/B 等）白占比可 >0.5，
            # 会被误判为黑字白底而整体反相（导致识别全错）。
            border = np.concatenate([char_bin[0, :], char_bin[-1, :],
                                     char_bin[:, 0], char_bin[:, -1]])
            if border.size and np.count_nonzero(border > 127) / border.size > 0.5:
                char_bin = cv2.bitwise_not(char_bin)
        # 二值化统一
        _, b = cv2.threshold(char_bin, 127, 255, cv2.THRESH_BINARY)
        ys = np.where(np.count_nonzero(b, axis=1) > 0)[0]
        xs = np.where(np.count_nonzero(b, axis=0) > 0)[0]
        if len(xs) and len(ys):
            b = b[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
        if b.size == 0:
            return np.zeros((self.char_h, self.char_w), dtype=np.uint8)
        # 等比缩放后居中放到 char_size 画布，保持长宽比
        scale = min((self.char_h - 4) / b.shape[0], (self.char_w - 4) / b.shape[1])
        scale = max(0.1, scale)
        nw, nh = max(1, int(b.shape[1] * scale)), max(1, int(b.shape[0] * scale))
        resized = cv2.resize(b, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((self.char_h, self.char_w), dtype=np.uint8)
        oy = (self.char_h - nh) // 2
        ox = (self.char_w - nw) // 2
        canvas[oy:oy + nh, ox:ox + nw] = resized
        return canvas
