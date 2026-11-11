"""CPU-only real-photo character classifier: nearest-neighbour NCC bank.

Idea A fallback when torch cannot be installed. The classifier is not synthetic:
every reference block comes from a real photo character crop
(EasyPR detector boxes + optional treeandgrass weak labels). Inference is the
same normalized-cross-correlation family as ``TemplateClassifier`` but uses the
full real-character bank instead of one prototype per class.

Artifact: ``server/lpr/models/real_char_knn.npz`` (+ ``.meta.json``), produced by
``tools/train_real_char_cnn.py``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import cv2
import numpy as np

from .charset import ALNUM, LETTERS, PROVINCES
from .classifier import HEAD_ALNUM, HEAD_HANZI, HEAD_LETTER

log = logging.getLogger("lpr.real_char_knn")

HEAD_ORDER = [HEAD_HANZI, HEAD_LETTER, HEAD_ALNUM]
HEAD_CHARS = {HEAD_HANZI: list(PROVINCES),
              HEAD_LETTER: list(LETTERS),
              HEAD_ALNUM: list(ALNUM)}


def _flatten(block: np.ndarray) -> np.ndarray:
    return block.astype(np.float32).ravel()


def _ncc_matrix(query: np.ndarray, bank: np.ndarray) -> np.ndarray:
    q = query - query.mean()
    qn = float(np.linalg.norm(q)) + 1e-6
    b = bank - bank.mean(axis=1, keepdims=True)
    bn = np.linalg.norm(b, axis=1) + 1e-6
    return (b @ q) / (bn * qn)


class RealCharKnn:
    """k-NN NCC classifier over real-photo character blocks."""

    def __init__(self, artifact: str | Path):
        self.path = Path(artifact)
        self.meta: dict = {}
        self.banks: dict[str, np.ndarray] = {}
        self.labels: dict[str, np.ndarray] = {}
        self.k = 1
        self.char_size = (32, 64)
        self._loaded = False
        self._calib: dict[str, tuple[float, float]] = {}

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with np.load(self.path, allow_pickle=False) as archive:
                for head in HEAD_ORDER:
                    self.banks[head] = archive[f"{head}_bank"].astype(np.float32)
                    self.labels[head] = archive[f"{head}_labels"].astype(np.int32)
            meta_path = self.path.with_suffix(".meta.json")
            if meta_path.exists():
                self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.char_size = tuple(self.meta.get("char_size", (32, 64)))
            self.k = int(self.meta.get("k", 1))
            for head in HEAD_ORDER:
                floor, ceil = self.meta.get("calibration", {}).get(head, [0.5, 0.92])
                self._calib[head] = (float(floor), float(ceil))
            self._loaded = True
            return True
        except Exception:  # noqa: BLE001
            log.exception("真实字符 kNN 模型加载失败: %s", self.path)
            self._loaded = False
            return False

    @property
    def available(self) -> bool:
        return self._loaded and any(v.size for v in self.banks.values())

    # ----------------------------- inference -----------------------------
    def _class_scores(self, block: np.ndarray, head: str) -> np.ndarray:
        bank = self.banks[head]
        labels = self.labels[head]
        n_classes = len(HEAD_CHARS[head])
        scores = np.full(n_classes, -1.0, dtype=np.float32)
        if bank.size == 0:
            return scores
        sims = _ncc_matrix(_flatten(block), bank)
        np.maximum.at(scores, labels, sims)
        return scores

    def _conf(self, head: str, similarity: float) -> float:
        floor, ceil = self._calib.get(head, (0.5, 0.92))
        value = (float(similarity) - floor) / max(1e-6, ceil - floor)
        return float(min(1.0, max(0.0, value)))

    def predict(self, char_img: np.ndarray, head: str, topk: int = 3) -> list[tuple[str, float]]:
        if not self._loaded:
            return []
        w, h = self.char_size
        img = char_img
        if (img.shape[1], img.shape[0]) != (w, h):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        scores = self._class_scores(img, head)
        if scores.size == 0:
            return []
        order = np.argsort(scores)[::-1][:topk]
        chars = HEAD_CHARS[head]
        return [(chars[int(i)], self._conf(head, float(scores[int(i)]))) for i in order]

    def confidence_for(self, char_img: np.ndarray, head: str, char: str) -> float:
        if not self._loaded or char not in HEAD_CHARS[head]:
            return 0.0
        w, h = self.char_size
        img = char_img
        if (img.shape[1], img.shape[0]) != (w, h):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        scores = self._class_scores(img, head)
        idx = HEAD_CHARS[head].index(char)
        return self._conf(head, float(scores[idx]))
