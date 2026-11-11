"""Reproducible synthetic style/angle/light benchmark using the cited MIT glyphs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core.config import get_config
from server.lpr.charset import DIGITS, LETTERS, PROVINCES, validate_grammar
from server.lpr.hyperlpr_adapter import HyperPlateRecognizer
from tools.generate_reference_plates import SOURCE, SOURCE_COMMIT

CONDITIONS = {
    "clean": {},
    "yaw_left": {"yaw": -32},
    "yaw_right": {"yaw": 32},
    "pitch": {"pitch": 24, "roll": -7},
    "dark": {"light": .38},
    "glare": {"glare": .45},
    "warm": {"bgr": [.70, .90, 1.15]},
    "cool": {"bgr": [1.15, .90, .70]},
    "dirty_blur": {"dirt": True, "blur": 1.0, "yaw": 16},
}


def scene(plate, params, seed):
    rng = np.random.default_rng(seed)
    ph, pw = plate.shape[:2]
    surface = plate.astype(np.float32)
    if params.get("dirt"):
        for _ in range(12):
            x, y = rng.integers([0, 0], [pw, ph])
            cv2.circle(surface, (int(x), int(y)), int(rng.integers(2, 6)),
                       (75, 85, 85), -1, cv2.LINE_AA)
    if params.get("glare"):
        yy, xx = np.mgrid[:ph, :pw]
        alpha = np.exp(-((xx - pw * .62) / (pw * .22)) ** 2 -
                       ((yy - ph * .35) / (ph * .8)) ** 2) * params["glare"]
        surface = surface * (1 - alpha[..., None]) + 255 * alpha[..., None]
    surface *= params.get("light", 1)
    surface *= np.array(params.get("bgr", [1, 1, 1]))
    surface = np.clip(surface, 0, 255).astype(np.uint8)
    yaw, pitch, roll = [math.radians(params.get(k, 0)) for k in ("yaw", "pitch", "roll")]
    ry = np.array([[math.cos(yaw), 0, math.sin(yaw)], [0, 1, 0],
                   [-math.sin(yaw), 0, math.cos(yaw)]])
    rx = np.array([[1, 0, 0], [0, math.cos(pitch), -math.sin(pitch)],
                   [0, math.sin(pitch), math.cos(pitch)]])
    rz = np.array([[math.cos(roll), -math.sin(roll), 0],
                   [math.sin(roll), math.cos(roll), 0], [0, 0, 1]])
    points = np.array([[-pw/2, -ph/2, 0], [pw/2, -ph/2, 0],
                       [pw/2, ph/2, 0], [-pw/2, ph/2, 0]]) @ (rz @ rx @ ry).T
    corners = (points[:, :2] * (900 / (points[:, 2:3] + 1050)) + [480, 365]).astype(np.float32)
    source = np.float32([[0, 0], [pw-1, 0], [pw-1, ph-1], [0, ph-1]])
    transform = cv2.getPerspectiveTransform(source, corners)
    image = np.clip(rng.normal(48, 3, (640, 960, 3)), 0, 255).astype(np.uint8)
    cv2.rectangle(image, (115, 145), (845, 525), (88, 91, 96), -1)
    cv2.rectangle(image, (150, 430), (810, 480), (35, 38, 42), -1)
    warped = cv2.warpPerspective(surface, transform, (960, 640))
    mask = cv2.warpPerspective(np.full((ph, pw), 255, np.uint8), transform, (960, 640))
    image[mask > 0] = warped[mask > 0]
    if params.get("blur"):
        image = cv2.GaussianBlur(image, (5, 5), params["blur"])
    return image, corners.tolist()


def generate(output: Path, per_style: int, seed: int):
    if per_style < 2:
        raise ValueError("per-style must be at least 2")
    commit = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    if commit != SOURCE_COMMIT:
        raise ValueError("reference source commit mismatch")
    if subprocess.check_output(["git", "-C", str(SOURCE), "status", "--porcelain", "--",
                                "font_model", "plate_model", "generate_multi_plate.py",
                                "plate_number.py", "LICENSE"], text=True).strip():
        raise ValueError("reference source has local modifications")
    sys.path.insert(0, str(SOURCE))
    from generate_multi_plate import MultiPlateGenerator
    renderer = MultiPlateGenerator(str(SOURCE / "plate_model"), str(SOURCE / "font_model"))
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    samples = []
    plates = set()
    for style in ("blue", "green_car", "green_truck"):
        for index in range(per_style):
            while True:
                prefix = rng.choice(PROVINCES) + rng.choice(LETTERS)
                number = "".join(rng.choices(DIGITS, k=5))
                if style == "blue":
                    number = "".join(rng.choices(DIGITS + LETTERS, k=5))
                elif style == "green_car":
                    number = rng.choice("DF") + number
                else:
                    number += rng.choice("DF")
                label = prefix + number
                if label not in plates:
                    plates.add(label)
                    break
            color = "blue" if style == "blue" else "green"
            if not validate_grammar(label, color)[0]:
                raise ValueError(label)
            plate = renderer.generate_plate_special(label, style, False, enhance=False)
            for condition, params in CONDITIONS.items():
                sample_seed = rng.randrange(2**31)
                image, corners = scene(plate, params, sample_seed)
                name = f"{style}-{index:03d}-{condition}.png"
                ok, encoded = cv2.imencode(".png", image)
                if not ok:
                    raise ValueError("PNG encoding failed")
                data = encoded.tobytes()
                path = output / name
                if path.exists() and path.read_bytes() != data:
                    raise ValueError(f"existing benchmark differs: {path}")
                path.write_bytes(data)
                samples.append({"sample_id": name[:-4], "file": name,
                                "sha256": hashlib.sha256(data).hexdigest(),
                                "plate": label, "style": style, "color": color,
                                "condition": condition, "parameters": params,
                                "seed": sample_seed, "corners": corners,
                                "source_kind": "synthetic", "pr2_eligible": False,
                                "split": "val" if index < per_style//2 else "test"})
    assets = sorted((SOURCE / "font_model").glob("*.jpg")) + sorted((SOURCE / "plate_model").glob("*.PNG"))
    manifest = {"format_version": 1, "source": "https://github.com/Pengfei8324/chinese_license_plate_generator",
                "source_commit": commit, "license": (SOURCE / "LICENSE").read_text(),
                "assets_hash": hashlib.sha256("".join(hashlib.sha256(p.read_bytes()).hexdigest()
                                                        for p in assets).encode()).hexdigest(),
                "seed": seed, "per_style": per_style, "samples": samples}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    return manifest


def evaluate(directory: Path, split: str, out: Path, baseline: bool = False):
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    from dataclasses import replace
    recognizer = HyperPlateRecognizer(replace(get_config().lpr, style_refinement=not baseline))
    rows = []
    groups = defaultdict(lambda: {"n": 0, "exact": 0, "wrong_auto": 0, "manual": 0})
    for sample in manifest["samples"]:
        if sample["split"] != split:
            continue
        path = directory / sample["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError("benchmark image hash mismatch")
        result = recognizer.recognize(str(path))
        row = {"sample_id": sample["sample_id"], "truth": sample["plate"],
               "prediction": result.plate, "confidence": result.overall_conf,
               "exact": sample["plate"] == result.plate,
               "wrong_auto": result.success and sample["plate"] != result.plate,
               "manual": result.need_manual, "elapsed_ms": result.elapsed_ms,
               "message": result.message}
        rows.append(row)
        for key in ("all", sample["style"], sample["condition"],
                    sample["style"]+":"+sample["condition"]):
            group = groups[key]
            group["n"] += 1
            for field in ("exact", "wrong_auto", "manual"):
                group[field] += int(row[field])
    report = {"manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "split": split, "synthetic": True, "pr2_eligible": False,
              "model_id": recognizer.model_id, "groups": dict(groups), "rows": rows}
    report["style_refinement"] = not baseline
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in groups.items() if ":" not in k}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "evaluate"))
    parser.add_argument("--directory", type=Path, default=ROOT / "datasets/external/style-benchmark-v1")
    parser.add_argument("--per-style", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--split", choices=("val", "test"), default="val")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    if args.action == "generate":
        print(json.dumps({"samples": len(generate(args.directory, args.per_style, args.seed)["samples"])}))
    else:
        if args.out is None:
            parser.error("evaluate requires --out")
        evaluate(args.directory, args.split, args.out, args.baseline)
