"""Train a real-photo character classifier for the license-plate verifier (Idea A).

Data policy
-----------
- Primary supervised signal: EasyPR GDTS photos with plate strings + rectangles
  (``datasets/real/easypr-manifest.json``; per-plate ``train``/``val``/``test`` splits,
  no plate is allowed to cross splits).
- Auxiliary real photos: treeandgrass candidate photos (no reviewed labels/bboxes in
  the manifest). They are used **only** as weak-labelled training data: HyperLPR3 must
  detect a plate, the detected text must equal the (unverified) filename label, the
  grammar must be valid and confidence must be high. EasyPR ``test`` stays frozen.
- No synthetic samples are generated or used for training.

Two interchangeable learners are produced from the same real crops:
- ``torch`` CNN (3 heads, same ONNX contract as ``server/lpr/classifier.py::OnnxClassifier``)
  exported to ``server/lpr/models/char_cnn_real.onnx``;
- ``ncc``: per-class real-character prototypes saved in a ``TemplateClassifier``-compatible
  directory ``server/lpr/models/real_char_templates/`` (always built; it is the automatic
  fallback and the safety comparison baseline).

Usage
-----
    python tools/train_real_char_cnn.py                 # auto backend, full data
    python tools/train_real_char_cnn.py --backend ncc   # CPU-only fallback
    python tools/train_real_char_cnn.py --limit 40 --epochs 3   # quick smoke

The script never mutates ``datasets/``; it only writes reports under
``_qa_audit/ideaA/`` and the two model artifacts under ``server/lpr/models/``.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from server.core.config import LPRConfig, PROJECT_ROOT as CFG_ROOT, load_config  # noqa: E402
from server.lpr.charset import normalize_valid_plate, validate_grammar  # noqa: E402
from server.lpr.classifier import HEAD_ALNUM, HEAD_CHARS, HEAD_HANZI, HEAD_LETTER  # noqa: E402
from server.lpr.rectifier import PlateRectifier  # noqa: E402
from server.lpr.recognizer import imread_unicode  # noqa: E402
from server.lpr.segmenter import CharSegmenter  # noqa: E402
from server.lpr.style_refiner import color_crop  # noqa: E402

HEAD_ORDER = [HEAD_HANZI, HEAD_LETTER, HEAD_ALNUM]
CHAR_INDEX = {head: {ch: i for i, ch in enumerate(HEAD_CHARS[head])} for head in HEAD_ORDER}
REPORT_DIR = PROJECT_ROOT.parent / "_qa_audit" / "ideaA"


def log(msg: str) -> None:
    print(f"[real-char] {msg}", flush=True)


@dataclass
class CharSample:
    image: np.ndarray
    head: str
    label: int
    char: str
    source: str
    split: str                 # train | val | test | train_aux
    sample_id: str
    plate: str
    position: int


@dataclass
class PlateCase:
    sample_id: str
    split: str
    plate: str
    color: str
    blocks: list = field(default_factory=list)
    expected: list = field(default_factory=list)
    source: str = "easypr"


# ---------------------------------------------------------------------------
# Plate crops
# ---------------------------------------------------------------------------
def crop_plate(image: np.ndarray, bbox, rotation_deg, color: str,
               cfg: LPRConfig) -> np.ndarray | None:
    """Crop the annotated rectangle, de-rotate and resize to the standard plate size."""
    h, w = image.shape[:2]
    if not bbox or len(bbox) != 4:
        return None
    x, y, bw, bh = (int(v) for v in bbox)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + bw), min(h, y + bh)
    if x1 <= x0 + 4 or y1 <= y0 + 4:
        return None
    crop = image[y0:y1, x0:x1]
    rect = PlateRectifier(blue_size=cfg.blue_size, green_size=cfg.green_size)
    if rotation_deg:
        crop = rect.rotate(crop, float(rotation_deg))
    size = rect.green_size if color == "green" else rect.blue_size
    return cv2.resize(crop, (int(size[0]), int(size[1])), interpolation=cv2.INTER_CUBIC)


def crop_from_bbox(image: np.ndarray, bbox, color: str,
                   cfg: LPRConfig) -> np.ndarray | None:
    """Plain detector-bbox crop resized to the standard plate size.

    Detector boxes are tight plate rectangles; a plain resize measured clearly
    better than colour-quad rectification for the low-resolution EasyPR photos.
    """
    h, w = image.shape[:2]
    if not bbox or len(bbox) != 4:
        return None
    x0, y0, x1, y1 = (int(v) for v in bbox)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 + 4 or y1 <= y0 + 4:
        return None
    size = (cfg.green_size if color == "green" else cfg.blue_size)
    return cv2.resize(image[y0:y1, x0:x1], (int(size[0]), int(size[1])),
                      interpolation=cv2.INTER_CUBIC)


def _detect_bbox(catcher, image, plate: str, *, color: str) -> tuple[list, bool]:
    """Return (best bbox, same_plate_as_truth) using HyperLPR3 detections."""
    found = catcher(image) or []
    best_conf = -1.0
    best_bbox = []
    same_bbox = []
    for detected, conf, ptype, bbox in found:
        try:
            normalized, _ = normalize_valid_plate(str(detected).replace("·", "").strip())
        except ValueError:
            normalized = None
        if normalized == plate:
            same_bbox = list(bbox)
            break
        if float(conf) > best_conf:
            best_conf, best_bbox = float(conf), list(bbox)
    return (same_bbox or best_bbox), bool(same_bbox)


def segment_plate(plate_img: np.ndarray, plate: str, color: str,
                  segmenter: CharSegmenter) -> list[np.ndarray] | None:
    if plate_img is None:
        return None
    blocks = segmenter.segment(plate_img, len(plate), color)
    return blocks if len(blocks) == len(plate) else None


def head_for_position(pos: int) -> str:
    return HEAD_HANZI if pos == 0 else HEAD_LETTER if pos == 1 else HEAD_ALNUM


def _manifest(rel: str) -> dict:
    return json.loads((PROJECT_ROOT / rel).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Dataset extraction
# ---------------------------------------------------------------------------
def extract_easypr(cfg: LPRConfig, limit: int = 0,
                    crop_source: str = "detector") -> tuple[list[CharSample], list[PlateCase], dict]:
    segmenter = CharSegmenter(tuple(cfg.char_size))
    catcher = None
    if crop_source == "detector":
        from hyperlpr3 import LicensePlateCatcher
        from hyperlpr3.common.typedef import DETECT_LEVEL_HIGH
        from server.lpr.hyperlpr_adapter import MODEL_DIR as _HL_MODEL_DIR, verify_models
        verify_models()
        catcher = LicensePlateCatcher(folder=str(_HL_MODEL_DIR.parent.parent),
                                      detect_level=DETECT_LEVEL_HIGH)
    manifest = _manifest("datasets/real/easypr-manifest.json")
    root = PROJECT_ROOT / "datasets/external/EasyPR"
    samples: list[CharSample] = []
    cases: list[PlateCase] = []
    failures: list[dict] = []
    counters = {"samples": 0, "image_unreadable": 0, "crop_failed": 0,
                "segment_failed": 0, "detect_failed": 0,
                "detected_plate_mismatch": 0}
    for row in manifest["samples"]:
        split = row["split"]
        if limit and sum(1 for c in cases if c.split == split) >= limit:
            continue
        counters["samples"] += 1
        sample_id = row["sample_id"]
        plate = row["plate"]
        color = row.get("color") or "blue"
        img = imread_unicode(str(root / row["file"]))
        if img is None:
            counters["image_unreadable"] += 1
            failures.append({"sample_id": sample_id, "reason": "image_unreadable"})
            continue
        if crop_source == "detector":
            bbox, same_plate = _detect_bbox(catcher, img, plate, color=color)
            if not bbox:
                counters["detect_failed"] += 1
                failures.append({"sample_id": sample_id, "reason": "detect_failed"})
                continue
            if not same_plate:
                counters["detected_plate_mismatch"] += 1
            plate_img = crop_from_bbox(img, bbox, color, cfg)
        else:
            plate_img = crop_plate(img, row.get("bbox_xywh"), row.get("rotation_deg"),
                                   color, cfg)
        if plate_img is None:
            counters["crop_failed"] += 1
            failures.append({"sample_id": sample_id, "reason": "crop_failed"})
            continue
        blocks = segment_plate(plate_img, plate, color, segmenter)
        if blocks is None:
            counters["segment_failed"] += 1
            failures.append({"sample_id": sample_id, "reason": "segment_failed",
                             "plate": plate})
            continue
        case = PlateCase(sample_id=sample_id, split=split, plate=plate, color=color)
        for pos, (block, ch) in enumerate(zip(blocks, plate)):
            head = head_for_position(pos)
            idx = CHAR_INDEX[head].get(ch)
            if idx is None:
                failures.append({"sample_id": sample_id, "reason": "char_not_in_head",
                                 "char": ch, "position": pos})
                continue
            samples.append(CharSample(image=block, head=head, label=idx, char=ch,
                                      source="easypr", split=split,
                                      sample_id=sample_id, plate=plate,
                                      position=pos))
            case.blocks.append(block)
            case.expected.append(ch)
        if len(case.blocks) == len(plate):
            cases.append(case)
    return samples, cases, {"counters": counters, "failures": failures,
                            "source": manifest.get("source"),
                            "source_commit": manifest.get("source_commit"),
                            "license": manifest.get("license")}


def _normalize_weak_label(raw: str) -> str | None:
    if not raw:
        return None
    text = "".join(ch for ch in str(raw) if ch not in "· ._-")
    try:
        plate, _ = normalize_valid_plate(text)
        return plate
    except ValueError:
        return None


def extract_treeandgrass(cfg: LPRConfig, limit: int = 300, min_conf: float = 0.98,
                         max_side: int = 1920) -> tuple[list[CharSample], dict]:
    """Weakly label treeandgrass photos with HyperLPR3 + filename, add to train_aux."""
    segmenter = CharSegmenter(tuple(cfg.char_size))
    manifest = _manifest("datasets/real/treeandgrass-manifest.json")
    root = PROJECT_ROOT / "datasets/external/treeandgrass"
    samples: list[CharSample] = []
    counters = {"attempted": 0, "accepted": 0, "no_detection": 0, "label_mismatch": 0,
                "low_confidence": 0, "segment_failed": 0, "image_unreadable": 0,
                "duplicate_sha": 0}
    failures: list[dict] = []
    seen_sha: set[str] = set()
    if limit and limit > 0:
        pool = manifest["samples"][:limit]
    else:
        pool = manifest["samples"]
    catcher = None
    import hashlib
    from hyperlpr3 import LicensePlateCatcher
    from hyperlpr3.common.typedef import DETECT_LEVEL_HIGH
    from server.lpr.hyperlpr_adapter import MODEL_HASHES, verify_models
    verify_models()
    from server.lpr.hyperlpr_adapter import MODEL_DIR as _HL_MODEL_DIR
    catcher = LicensePlateCatcher(folder=str(_HL_MODEL_DIR.parent.parent),
                                  detect_level=DETECT_LEVEL_HIGH)
    for row in pool:
        counters["attempted"] += 1
        digest = row.get("sha256")
        if digest and digest in seen_sha:
            counters["duplicate_sha"] += 1
            continue
        if digest:
            seen_sha.add(digest)
        img = imread_unicode(str(root / row["file"]))
        if img is None:
            counters["image_unreadable"] += 1
            continue
        if max_side and max(img.shape[:2]) > max_side:
            scale = max_side / float(max(img.shape[:2]))
            img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)))
        try:
            found = catcher(img)
        except Exception as exc:  # noqa: BLE001
            failures.append({"sample_id": row["sample_id"], "reason": f"detect_error:{exc}"})
            continue
        label = _normalize_weak_label(row.get("filename_label") or "")
        if not label:
            counters["label_mismatch"] += 1
            continue
        best = None
        for plate, conf, ptype, bbox in found or []:
            text = str(plate).replace("·", "").strip()
            try:
                normalized, color = normalize_valid_plate(text)
            except ValueError:
                continue
            if label and normalized != label:
                continue
            valid, _ = validate_grammar(normalized, color)
            if not valid or float(conf) < min_conf:
                continue
            if best is None or float(conf) > best[1]:
                best = (normalized, float(conf), color, bbox)
        if best is None:
            if label is None:
                counters["label_mismatch"] += 1
            elif found:
                counters["low_confidence"] += 1
            else:
                counters["no_detection"] += 1
            continue
        plate, conf, color, bbox = best
        crop = color_crop(img, bbox)
        if crop is None:
            crop = crop_plate(img, bbox, None, color, cfg)
        if crop is None:
            counters["segment_failed"] += 1
            continue
        size = (cfg.green_size if color == "green" else cfg.blue_size)
        crop = cv2.resize(crop, (int(size[0]), int(size[1])), interpolation=cv2.INTER_CUBIC)
        blocks = segment_plate(crop, plate, color, segmenter)
        if blocks is None:
            counters["segment_failed"] += 1
            failures.append({"sample_id": row["sample_id"], "reason": "segment_failed",
                             "plate": plate})
            continue
        for pos, (block, ch) in enumerate(zip(blocks, plate)):
            head = head_for_position(pos)
            idx = CHAR_INDEX[head].get(ch)
            if idx is None:
                continue
            samples.append(CharSample(image=block, head=head, label=idx, char=ch,
                                      source="treeandgrass_weak", split="train_aux",
                                      sample_id=row["sample_id"], plate=plate,
                                      position=pos))
        counters["accepted"] += 1
    return samples, {"counters": counters, "failures": failures,
                     "source": manifest.get("source"),
                     "source_commit": manifest.get("source_commit"),
                     "repository_license": manifest.get("repository_license"),
                     "weak_label_rule": "filename_label == HyperLPR3 detection, "
                                        f"conf>={min_conf}, grammar valid; train_aux only"}


# ---------------------------------------------------------------------------
# NCC (real character template library)
# ---------------------------------------------------------------------------
def save_real_templates(samples: Iterable[CharSample], out_dir: Path,
                        char_size: tuple[int, int], provenance: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    heads: dict[str, list[str]] = {}
    files: dict[str, list[str]] = {}
    counts: dict[str, dict[str, int]] = {}
    total = 0
    for head in HEAD_ORDER:
        by_char: dict[str, list[np.ndarray]] = {}
        for s in samples:
            if s.head == head and s.split in ("train", "train_aux"):
                by_char.setdefault(s.char, []).append(s.image)
        chars = [ch for ch in HEAD_CHARS[head] if ch in by_char]
        heads[head] = chars
        files[head] = []
        counts[head] = {ch: len(by_char[ch]) for ch in chars}
        for i, ch in enumerate(chars):
            stack = np.stack([img.astype(np.float32) for img in by_char[ch]])
            prototype = np.median(stack, axis=0)
            prototype = np.clip(prototype, 0, 255).astype(np.uint8)
            if prototype.shape[:2] != (char_size[1], char_size[0]):
                prototype = cv2.resize(prototype, char_size, interpolation=cv2.INTER_AREA)
            fn = f"{head}_{i:03d}.png"
            cv2.imwrite(str(out_dir / fn), prototype)
            files[head].append(fn)
            total += 1
    meta = {"char_size": list(char_size), "heads": heads, "files": files,
            "source": "real_photo_characters_only",
            "image_counts": counts,
            "provenance": provenance}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return {"templates": total,
            "coverage": {h: len(heads[h]) for h in HEAD_ORDER},
            "counts": counts}


def save_real_knn(samples: Iterable[CharSample], out_npz: Path,
                  char_size: tuple[int, int], provenance: dict,
                  k: int = 1) -> dict:
    """Persist the real-photo k-NN NCC bank consumed by ``RealCharKnn``."""
    out_npz.parent.mkdir(parents=True, exist_ok=True)
    w, h = (int(char_size[0]), int(char_size[1]))
    arrays: dict[str, np.ndarray] = {}
    counts: dict[str, dict[str, int]] = {}
    total = 0
    for head in HEAD_ORDER:
        rows = [s for s in samples if s.head == head and s.split in ("train", "train_aux")]
        if not rows:
            arrays[f"{head}_bank"] = np.zeros((0, w * h), np.float32)
            arrays[f"{head}_labels"] = np.zeros((0,), np.int32)
            counts[head] = {}
            continue
        per_class: dict[int, int] = {}
        for s in rows:
            per_class[s.label] = per_class.get(s.label, 0) + 1
        counts[head] = {HEAD_CHARS[head][label]: n for label, n in sorted(per_class.items())}
        arrays[f"{head}_bank"] = np.stack([flatten_block(s.image) for s in rows])
        arrays[f"{head}_labels"] = np.array([s.label for s in rows], dtype=np.int32)
        total += len(rows)
    np.savez_compressed(out_npz, **arrays)
    meta = {"format": "real-char-knn-v1", "char_size": list(char_size), "k": int(k),
            "source": "real_photo_characters_only", "counts": counts,
            "bank_size": total, "provenance": provenance}
    out_npz.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(out_npz), "bank_size": total,
            "coverage": {head: len(counts.get(head, {})) for head in HEAD_ORDER},
            "counts": counts}


def flatten_block(block: np.ndarray) -> np.ndarray:
    return block.astype(np.float32).ravel()


def calibrate_knn(npz_path: Path, val_samples: list[CharSample]) -> dict:
    """Calibrate similarity→confidence mapping on EasyPR val characters only."""
    from server.lpr.real_char_knn import RealCharKnn
    clf = RealCharKnn(npz_path)
    if not clf.load():
        return {}
    calibration: dict[str, list[float]] = {}
    for head in HEAD_ORDER:
        head_samples = [s for s in val_samples if s.head == head]
        if not head_samples or not clf.banks.get(head, np.zeros(0)).size:
            calibration[head] = [0.55, 0.92]
            continue
        sims = []
        for s in head_samples:
            scores = clf._class_scores(s.image, head)
            expected = HEAD_CHARS[head].index(s.char)
            sims.append(float(scores[expected]))
        arr = np.array(sims, dtype=np.float32)
        calibration[head] = [round(float(np.percentile(arr, 10)), 4),
                             round(float(np.percentile(arr, 90)), 4)]
    meta_path = npz_path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["calibration"] = calibration
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return calibration


def _eval_with_knn(npz_path: Path, char_size, val_cases, test_cases,
                   auto_min_conf: float) -> dict:
    from server.lpr.real_char_knn import RealCharKnn
    clf = RealCharKnn(npz_path)
    available = clf.load()
    return {"available": available,
            "val": evaluate_classifier(clf, val_cases, auto_min_conf=auto_min_conf),
            "test": evaluate_classifier(clf, test_cases, auto_min_conf=auto_min_conf)}


# ---------------------------------------------------------------------------
# Evaluation (reconstruction path only; no end-to-end gate is claimed here)
# ---------------------------------------------------------------------------
def _plate_cases_by_split(cases: list[PlateCase], split: str) -> list[PlateCase]:
    return [c for c in cases if c.split == split]


def evaluate_classifier(classifier, cases: list[PlateCase], *,
                        auto_min_conf: float = 0.95) -> dict:
    """Score a character classifier on already-segmented real plate crops."""
    total_chars = correct_chars = 0
    exact = 0
    auto = 0
    wrong_auto = 0
    per_position_correct = [0] * 8
    per_position_total = [0] * 8
    per_char: dict[str, dict[str, int]] = {}
    rows = []
    for case in cases:
        pred_chars: list[str] = []
        confs: list[float] = []
        for pos, (block, expected) in enumerate(zip(case.blocks, case.expected)):
            head = head_for_position(pos)
            top = classifier.predict(block, head, topk=3)
            pred = top[0][0] if top else ""
            expected_conf = next((float(p) for ch, p in top if ch == expected), 0.0)
            pred_chars.append(pred)
            confs.append(expected_conf)
            total_chars += 1
            ok = pred == expected
            correct_chars += int(ok)
            per_position_correct[pos] += int(ok)
            per_position_total[pos] += 1
            bucket = per_char.setdefault(expected, {"n": 0, "correct": 0})
            bucket["n"] += 1
            bucket["correct"] += int(ok)
        got = "".join(pred_chars)
        is_exact = got == case.plate
        exact += int(is_exact)
        min_conf = min(confs) if confs else 0.0
        is_auto = len(pred_chars) == len(case.plate) and min_conf >= auto_min_conf
        auto += int(is_auto)
        wrong_auto += int(is_auto and not is_exact)
        rows.append({"sample_id": case.sample_id, "truth": case.plate,
                     "prediction": got, "exact": is_exact,
                     "min_conf": round(min_conf, 4),
                     "auto_pass": is_auto,
                     "wrong_auto_pass": bool(is_auto and not is_exact),
                     "source": case.source})
    n = len(cases)
    return {
        "samples": n,
        "plate_exact": exact,
        "plate_exact_rate": exact / n if n else 0.0,
        "char_total": total_chars,
        "char_correct": correct_chars,
        "char_accuracy": correct_chars / total_chars if total_chars else 0.0,
        "auto_pass": auto,
        "auto_pass_rate": auto / n if n else 0.0,
        "wrong_auto_pass": wrong_auto,
        "auto_min_conf": auto_min_conf,
        "per_position": {str(i): {"n": per_position_total[i],
                                  "accuracy": (per_position_correct[i] /
                                               per_position_total[i]
                                               if per_position_total[i] else None)}
                         for i in range(8) if per_position_total[i]},
        "per_char": per_char,
        "rows": rows,
    }


def compare_classifiers(template_dir: Path, onnx_model: Path,
                        char_size: tuple[int, int], val_cases, test_cases) -> dict:
    from server.lpr.classifier import OnnxClassifier, TemplateClassifier
    results: dict = {}
    tc = TemplateClassifier(template_dir, char_size)
    tc.load()
    results["ncc"] = {"available": tc.available,
                      "val": evaluate_classifier(tc, val_cases),
                      "test": evaluate_classifier(tc, test_cases)}
    oc = OnnxClassifier(onnx_model, char_size)
    if oc.load():
        results["torch_onnx"] = {"available": True,
                                "val": evaluate_classifier(oc, val_cases),
                                "test": evaluate_classifier(oc, test_cases)}
    else:
        results["torch_onnx"] = {"available": False}
    return results


# ---------------------------------------------------------------------------
# Torch training
# ---------------------------------------------------------------------------
def train_torch(samples: list[CharSample], char_size: tuple[int, int], *,
                epochs: int, batch_size: int, lr: float, seed: int,
                out_model: Path, report_dir: Path) -> dict:
    try:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F  # noqa: N812
    except ImportError as exc:
        return {"available": False, "reason": f"torch unavailable: {exc}"}

    torch.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    w, h = int(char_size[0]), int(char_size[1])
    log(f"torch {torch.__version__}  device={device}")
    by_head: dict[str, dict[str, list[CharSample]]] = {
        "train": {}, "val": {}}
    for split in ("train", "val"):
        for s in samples:
            if s.split == split:
                by_head[split].setdefault(s.head, []).append(s)
    # merge train_aux into train only
    for s in samples:
        if s.split == "train_aux":
            by_head["train"].setdefault(s.head, []).append(s)

    class CharCNN(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
                nn.AdaptiveAvgPool2d((2, 2)),
            )
            self.heads = nn.ModuleList([
                nn.Sequential(nn.Linear(128 * 2 * 2, 128), nn.ReLU(),
                              nn.Dropout(0.3), nn.Linear(128, len(HEAD_CHARS[head])))
                for head in HEAD_ORDER
            ])

        def forward(self, x):
            x = self.features(x)
            x = torch.flatten(x, 1)
            return [head(x) for head in self.heads]

    model = CharCNN().to(device)
    # Unseen classes must never win argmax; seen classes get bias 0.
    seen = {head: {s.label for s in by_head["train"].get(head, [])} for head in HEAD_ORDER}
    with torch.no_grad():
        for head_index, head in enumerate(HEAD_ORDER):
            bias = model.heads[head_index][-1].bias
            for idx in range(len(HEAD_CHARS[head])):
                bias[idx] = 0.0 if idx in seen[head] else -12.0

    data = {}
    for head in HEAD_ORDER:
        rows = by_head["train"].get(head, [])
        if not rows:
            continue
        X = torch.from_numpy(np.stack([s.image for s in rows]).astype(np.float32)
                             / 255.0).unsqueeze(1)
        y = torch.tensor([s.label for s in rows], dtype=torch.long)
        counts = torch.bincount(y, minlength=len(HEAD_CHARS[head])).float()
        weights = torch.zeros_like(counts)
        present = counts > 0
        weights[present] = counts[present].sum() / (present.sum() * counts[present])
        val_rows = by_head["val"].get(head, [])
        val_X = (torch.from_numpy(np.stack([s.image for s in val_rows]).astype(np.float32)
                                  / 255.0).unsqueeze(1) if val_rows else None)
        val_y = (torch.tensor([s.label for s in val_rows], dtype=torch.long)
                 if val_rows else None)
        data[head] = {"X": X, "y": y, "w": weights, "Xv": val_X, "yv": val_y}
    model.zero_grad()
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    head_index = {head: i for i, head in enumerate(HEAD_ORDER)}
    best = {"val_char_accuracy": -1.0, "state": None, "epoch": 0}
    history = []
    for epoch in range(1, epochs + 1):
        model.train()
        losses = []
        for head in HEAD_ORDER:
            if head not in data:
                continue
            d = data[head]
            X, y, weights = d["X"].to(device), d["y"].to(device), d["w"].to(device)
            n = X.shape[0]
            idx = torch.randperm(n, device=device)
            for start in range(0, n, batch_size):
                sel = idx[start:start + batch_size]
                outs = model(X[sel])
                loss = F.cross_entropy(outs[head_index[head]], y[sel],
                                       weight=weights)
                opt.zero_grad()
                loss.backward()
                opt.step()
                losses.append(float(loss.item()))
        model.eval()
        val_correct = val_total = 0
        with torch.no_grad():
            for head in HEAD_ORDER:
                d = data.get(head)
                if d is None or d["Xv"] is None:
                    continue
                logits = model(d["Xv"].to(device))[head_index[head]]
                pred = logits.argmax(1)
                val_correct += int((pred.cpu() == d["yv"]).sum())
                val_total += int(d["yv"].shape[0])
        val_acc = val_correct / val_total if val_total else 0.0
        history.append({"epoch": epoch,
                        "loss": sum(losses) / max(1, len(losses)),
                        "val_char_accuracy": val_acc})
        log(f"epoch {epoch:>3}/{epochs} loss={history[-1]['loss']:.4f} "
            f"val_char_acc={val_acc:.4f}")
        if val_acc > best["val_char_accuracy"]:
            best.update({"val_char_accuracy": val_acc, "epoch": epoch,
                         "state": {k: v.detach().clone()
                                   for k, v in model.state_dict().items()}})
    if best["state"] is not None:
        model.load_state_dict(best["state"])
    model.eval()
    out_model.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 1, h, w, device=device)
    torch.onnx.export(model, dummy, str(out_model), input_names=["input"],
                      output_names=list(HEAD_ORDER),
                      dynamic_axes={"input": {0: "batch"},
                                    **{name: {0: "batch"} for name in HEAD_ORDER}},
                      opset_version=13, do_constant_folding=True)
    state_path = report_dir / "char_cnn_real_state.pt"
    torch.save({k: v.cpu() for k, v in model.state_dict().items()}, state_path)
    log(f"ONNX -> {out_model}  best_epoch={best['epoch']} "
        f"val_char_acc={best['val_char_accuracy']:.4f}")
    return {"available": True, "torch_version": torch.__version__,
            "device": str(device), "epochs": epochs,
            "best_epoch": best["epoch"],
            "val_char_accuracy": best["val_char_accuracy"],
            "train_counts": {head: len(by_head["train"].get(head, []))
                             for head in HEAD_ORDER},
            "val_counts": {head: len(by_head["val"].get(head, [])) for head in HEAD_ORDER},
            "history": history,
            "onnx": str(out_model), "state": str(state_path)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Train/evaluate a real-photo character classifier")
    ap.add_argument("--backend", choices=("auto", "torch", "ncc"), default="auto")
    ap.add_argument("--limit", type=int, default=0, help="max EasyPR samples per split (0=all)")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--treeandgrass-limit", type=int, default=300,
                    help="max treeandgrass photos for weak labels (0=all 1222)")
    ap.add_argument("--treeandgrass-min-conf", type=float, default=0.98)
    ap.add_argument("--auto-min-conf", type=float, default=0.95,
                    help="reconstruction auto-pass char confidence (safety analysis only)")
    ap.add_argument("--out-model", type=Path,
                    default=PROJECT_ROOT / "server/lpr/models/char_cnn_real.onnx")
    ap.add_argument("--out-templates", type=Path,
                    default=PROJECT_ROOT / "server/lpr/models/real_char_templates")
    ap.add_argument("--out-knn", type=Path,
                    default=PROJECT_ROOT / "server/lpr/models/real_char_knn.npz")
    ap.add_argument("--knn-k", type=int, default=1)
    ap.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    ap.add_argument("--crop-source", choices=("detector", "annotation"),
                    default="detector",
                    help="plate crop source for EasyPR supervised chars")
    ap.add_argument("--no-treeandgrass", action="store_true")
    args = ap.parse_args()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    app_cfg = load_config(force=True)
    cfg = app_cfg.lpr
    char_size = tuple(int(v) for v in cfg.char_size)
    log(f"char_size(W,H)={char_size} backend={args.backend} limit={args.limit}")

    easy_samples, plate_cases, easypr_meta = extract_easypr(
        cfg, limit=args.limit, crop_source=args.crop_source)
    report: dict = {"config": {k: (str(v) if isinstance(v, Path) else v)
                               for k, v in vars(args).items()},
                    "easypr": easypr_meta,
                    "easypr_char_counts": _counts(easy_samples)}
    log(f"EasyPR char samples={len(easy_samples)} cases={len(plate_cases)} "
        f"failures={len(easypr_meta['failures'])}")

    tree_samples: list[CharSample] = []
    if not args.no_treeandgrass:
        try:
            tree_samples, tree_meta = extract_treeandgrass(
                cfg, limit=args.treeandgrass_limit, min_conf=args.treeandgrass_min_conf)
        except Exception as exc:  # noqa: BLE001
            tree_meta = {"error": f"{type(exc).__name__}: {exc}"}
            log(f"treeandgrass weak labels unavailable: {tree_meta['error']}")
        report["treeandgrass"] = tree_meta
        report["treeandgrass_char_counts"] = _counts(tree_samples)
        log(f"treeandgrass weak char samples={len(tree_samples)}")
    all_samples = easy_samples + tree_samples

    # Verify split isolation (EasyPR plates must not cross train/val/test).
    split_plates: dict[str, set] = {}
    for c in plate_cases:
        split_plates.setdefault(c.split, set()).add(c.plate)
    overlaps = []
    for a, b in (("train", "val"), ("train", "test"), ("val", "test")):
        inter = split_plates.get(a, set()) & split_plates.get(b, set())
        if inter:
            overlaps.append({"pair": [a, b], "plates": sorted(inter)[:20],
                             "count": len(inter)})
    report["split_isolation"] = {"overlaps": overlaps,
                                 "counts": {k: len(v) for k, v in split_plates.items()}}

    provenance = {"easypr": {k: easypr_meta.get(k) for k in
                             ("source", "source_commit", "license")},
                  "treeandgrass": {k: report.get("treeandgrass", {}).get(k)
                                   for k in ("source", "source_commit",
                                             "repository_license", "weak_label_rule")}}
    built = save_real_templates(all_samples, args.out_templates, char_size, provenance)
    report["real_templates"] = built
    log(f"real NCC templates={built['templates']} coverage={built['coverage']}")

    knn_built = save_real_knn(all_samples, args.out_knn, char_size, provenance,
                              k=args.knn_k)
    calibration = calibrate_knn(args.out_knn, [s for s in easy_samples
                                               if s.split == "val"])
    knn_built["calibration"] = calibration
    report["real_knn"] = knn_built
    log(f"real kNN bank={knn_built['bank_size']} coverage={knn_built['coverage']} "
        f"calibration={calibration}")

    val_cases = _plate_cases_by_split(plate_cases, "val")
    test_cases = _plate_cases_by_split(plate_cases, "test")
    # Train split is used to build templates/bank; evaluate only val/test.
    ncc_eval = _eval_with_templates(args.out_templates, char_size, val_cases, test_cases,
                                    args.auto_min_conf)
    report["ncc"] = ncc_eval
    log(f"NCC val plate_exact={ncc_eval['val']['plate_exact_rate']:.3f} "
        f"test plate_exact={ncc_eval['test']['plate_exact_rate']:.3f} "
        f"test char_acc={ncc_eval['test']['char_accuracy']:.3f}")

    knn_eval = _eval_with_knn(args.out_knn, char_size, val_cases, test_cases,
                              args.auto_min_conf)
    report["knn"] = knn_eval
    log(f"kNN val plate_exact={knn_eval['val']['plate_exact_rate']:.3f} "
        f"test plate_exact={knn_eval['test']['plate_exact_rate']:.3f} "
        f"test char_acc={knn_eval['test']['char_accuracy']:.3f}")

    torch_result = {"available": False, "reason": "not attempted"}
    if args.backend in ("auto", "torch"):
        try:
            torch_result = train_torch(all_samples, char_size,
                                       epochs=args.epochs, batch_size=args.batch_size,
                                       lr=args.lr, seed=args.seed,
                                       out_model=args.out_model,
                                       report_dir=args.report_dir)
        except Exception as exc:  # noqa: BLE001
            torch_result = {"available": False,
                            "reason": f"{type(exc).__name__}: {exc}"}
    report["torch"] = torch_result
    if torch_result.get("available"):
        onnx_eval = _eval_with_onnx(args.out_model, char_size, val_cases, test_cases,
                                    args.auto_min_conf)
        report["torch_onnx"] = onnx_eval
        log(f"ONNX val plate_exact={onnx_eval['val']['plate_exact_rate']:.3f} "
            f"test plate_exact={onnx_eval['test']['plate_exact_rate']:.3f} "
            f"test char_acc={onnx_eval['test']['char_accuracy']:.3f}")
        better = (onnx_eval["val"]["plate_exact_rate"] >
                  knn_eval["val"]["plate_exact_rate"] + 0.02)
        report["selected_backend"] = "torch_onnx" if better else "real_knn"
    else:
        report["selected_backend"] = "real_knn"
        log("torch unavailable/not trained; real-photo kNN/NCC fallback is primary: "
            f"{torch_result.get('reason')}")
    report["duration_sec"] = round(time.time() - started, 2)
    report["data_policy"] = {
        "synthetic_training": "none",
        "easypr_splits": "manifest train/val/test, per-plate isolation",
        "treeandgrass": "weak labels only for train_aux (not val/test)",
        "pr2_eligible": False,
        "pr2_ineligible_reason": "Real photos are small EasyPR blue-plate subset plus "
                                 "weak-labelled candidates; no frozen PR-2 strata."}
    out = args.report_dir / "train_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"report -> {out}")
    return 0


def _counts(samples: Iterable[CharSample]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for s in samples:
        out.setdefault(s.split, {}).setdefault(s.source, 0)
        out[s.split][s.source] += 1
    return out


def _eval_with_templates(template_dir: Path, char_size, val_cases, test_cases,
                         auto_min_conf: float) -> dict:
    from server.lpr.classifier import TemplateClassifier
    clf = TemplateClassifier(template_dir, char_size)
    clf.load()
    return {"available": bool(clf.available),
            "val": evaluate_classifier(clf, val_cases, auto_min_conf=auto_min_conf),
            "test": evaluate_classifier(clf, test_cases, auto_min_conf=auto_min_conf)}


def _eval_with_onnx(model_path: Path, char_size, val_cases, test_cases,
                    auto_min_conf: float) -> dict:
    from server.lpr.classifier import OnnxClassifier
    clf = OnnxClassifier(model_path, char_size)
    available = clf.load()
    return {"available": available,
            "val": evaluate_classifier(clf, val_cases, auto_min_conf=auto_min_conf),
            "test": evaluate_classifier(clf, test_cases, auto_min_conf=auto_min_conf)}


if __name__ == "__main__":
    raise SystemExit(main())



