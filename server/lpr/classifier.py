"""字符识别分类器（lpr/classifier.py）——流水线 ⑥。

可插拔两种后端（RR-4.2 模块解耦，可单独替换/测试）：
- ``TemplateClassifier``：归一化互相关（NCC）模板匹配，课程所列"模板匹配"方法，
  自包含、开箱即用（模板由 tools/gen_templates.py 用号牌规范字体渲染生成）；
- ``OnnxClassifier``：加载自训 CNN 导出的 ``char_cnn.onnx``（课程"神经网络"方法，
  训练脚本 tools/train_char_cnn.py），跨平台用 ONNX Runtime 推理。

三个输出头：
- ``hanzi``：31 省份汉字；``letter``：24 字母(A–Z 去 I/O)；``alnum``：34 字母数字。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Protocol

import cv2
import numpy as np

from . import charset

log = logging.getLogger("lpr.classifier")

HEAD_HANZI = "hanzi"
HEAD_LETTER = "letter"
HEAD_ALNUM = "alnum"

HEAD_CHARS = {
    HEAD_HANZI: list(charset.PROVINCES),
    HEAD_LETTER: list(charset.LETTERS),
    HEAD_ALNUM: list(charset.ALNUM),
}


class CharClassifier(Protocol):
    def predict(self, char_img: np.ndarray, head: str, topk: int = 3) -> list[tuple[str, float]]:
        """返回 [(字符, 置信度)]，按置信度降序。"""
        ...


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """归一化互相关（零均值单位范数点积），范围 [-1, 1]。"""
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    a = a - a.mean()
    b = b - b.mean()
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - np.max(x)
    e = np.exp(x)
    return e / (np.sum(e) + 1e-9)


class TemplateClassifier:
    """模板匹配分类器（NCC）。"""

    def __init__(self, template_dir: str | Path, char_size=(20, 40)):
        self.dir = Path(template_dir)
        self.char_size = (int(char_size[0]), int(char_size[1]))
        self._templates: dict[str, list[tuple[str, np.ndarray]]] = {}
        self._loaded = False

    def load(self) -> bool:
        meta_path = self.dir / "meta.json"
        if not meta_path.exists():
            log.warning("模板未生成：%s（请运行 tools/gen_templates.py）", meta_path)
            return False
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self._templates = {}
        for head, chars in meta["heads"].items():
            files = meta["files"][head]
            items = []
            for ch, fn in zip(chars, files):
                # Windows 下 cv2.imread 不能读取含中文的父路径；与 recognizer.imread_unicode 等价。
                _data = np.fromfile(str(self.dir / fn), dtype=np.uint8)
                img = cv2.imdecode(_data, cv2.IMREAD_GRAYSCALE) if _data.size else None
                if img is None:
                    continue
                if (img.shape[1], img.shape[0]) != self.char_size:
                    img = cv2.resize(img, self.char_size, interpolation=cv2.INTER_AREA)
                items.append((ch, img))
            self._templates[head] = items
        self._loaded = True
        return True

    @property
    def available(self) -> bool:
        return self._loaded and bool(self._templates)

    def predict(self, char_img: np.ndarray, head: str, topk: int = 3) -> list[tuple[str, float]]:
        if not self._loaded:
            self.load()
        items = self._templates.get(head, [])
        if not items:
            return []
        if (char_img.shape[1], char_img.shape[0]) != self.char_size:
            char_img = cv2.resize(char_img, self.char_size, interpolation=cv2.INTER_AREA)
        scores = [(ch, _ncc(char_img, tmpl)) for ch, tmpl in items]
        # NCC ∈ [-1,1] → 置信度。用线性标定而非 (s+1)/2：
        # 后者会把 NCC=0.6 的错配也映到 0.8 的"高置信"，导致错牌被自动放行；
        # 标定后只有 NCC≥~0.9 的真匹配才达到自动放行阈值，不确定的转人工（RR-1.2）。
        normed = [(ch, float(min(1.0, max(0.0, (s - 0.45) / 0.55)))) for ch, s in scores]
        normed.sort(key=lambda t: t[1], reverse=True)
        return normed[:topk]


class OnnxClassifier:
    """ONNX Runtime 推理（自训 CNN）。模型输出 3 个头，顺序 [hanzi, letter, alnum]。"""

    def __init__(self, model_path: str | Path, char_size=(20, 40)):
        self.model_path = Path(model_path)
        self.char_size = (int(char_size[0]), int(char_size[1]))
        self._sess = None
        self._input_name: Optional[str] = None
        self._output_names: list[str] = []
        self._head_index = {HEAD_HANZI: 0, HEAD_LETTER: 1, HEAD_ALNUM: 2}

    def load(self) -> bool:
        if not self.model_path.exists():
            return False
        try:
            import onnxruntime as ort
            self._sess = ort.InferenceSession(str(self.model_path),
                                              providers=["CPUExecutionProvider"])
            self._input_name = self._sess.get_inputs()[0].name
            self._output_names = [o.name for o in self._sess.get_outputs()]
            return True
        except Exception:  # noqa: BLE001
            log.exception("ONNX 模型加载失败，将回退模板匹配")
            self._sess = None
            return False

    @property
    def available(self) -> bool:
        return self._sess is not None

    def predict(self, char_img: np.ndarray, head: str, topk: int = 3) -> list[tuple[str, float]]:
        if self._sess is None:
            return []
        w, h = self.char_size
        img = char_img
        if (img.shape[1], img.shape[0]) != (w, h):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        arr = img.astype(np.float32) / 255.0
        arr = arr.reshape(1, 1, h, w)   # NCHW，单通道
        try:
            outs = self._sess.run(self._output_names, {self._input_name: arr})
        except Exception:  # noqa: BLE001
            log.exception("ONNX 推理失败")
            return []
        idx = self._head_index.get(head, 2)
        idx = min(idx, len(outs) - 1)
        logits = np.asarray(outs[idx]).ravel()
        chars = HEAD_CHARS[head]
        probs = _softmax(logits[:len(chars)])
        order = np.argsort(probs)[::-1][:topk]
        return [(chars[i], float(probs[i])) for i in order]


def build_classifier(backend: str, onnx_model: str | Path, template_dir: str | Path,
                     char_size=(20, 40)) -> CharClassifier:
    """按配置选择分类器后端：auto=有 onnx 用 onnx，否则模板；template/onnx 强制。"""
    if backend in ("auto", "onnx"):
        oc = OnnxClassifier(onnx_model, char_size)
        if oc.load():
            log.info("字符分类器后端：ONNX (%s)", onnx_model)
            return oc
        if backend == "onnx":
            log.warning("强制 onnx 但模型不可用，回退模板匹配")
    tc = TemplateClassifier(template_dir, char_size)
    tc.load()
    log.info("字符分类器后端：模板匹配 (available=%s)", tc.available)
    return tc
