"""Generate traceable blue/green auxiliary plates from the cited MIT assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "datasets/external/chinese_license_plate_generator"
SOURCE_COMMIT = "df4757f99e63d7180a59d6c2633f466a339b500b"
OUTPUT = PROJECT_ROOT / "datasets/external/reference-synthetic"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.lpr.charset import generate_plate_type, validate_grammar  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(source: Path, output: Path, *, per_color: int, seed: int) -> dict:
    if per_color < 1:
        raise ValueError("per_color must be positive")
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != SOURCE_COMMIT:
        raise ValueError(f"source commit differs: {commit}")
    if subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain", "--",
             "font_model", "plate_model", "generate_multi_plate.py", "plate_number.py",
             "LICENSE"], text=True).strip():
        raise ValueError("source assets have uncommitted changes")
    license_text = (source / "LICENSE").read_text(encoding="utf-8")
    if "MIT License" not in license_text or "Copyright (c) 2020 leijd" not in license_text:
        raise ValueError("source MIT license missing or changed")
    sys.path.insert(0, str(source))
    from generate_multi_plate import MultiPlateGenerator  # noqa: E402

    renderer = MultiPlateGenerator(str(source / "plate_model"), str(source / "font_model"))
    rng = random.Random(seed)
    output.mkdir(parents=True, exist_ok=True)
    samples = []
    used = set()
    for color, base in (("blue", "blue"), ("green", "green_car")):
        for index in range(per_color):
            while True:
                plate = generate_plate_type(color, rng)
                if plate not in used:
                    used.add(plate)
                    break
            valid, reason = validate_grammar(plate, color)
            if not valid:
                raise ValueError(f"generated invalid plate {plate}: {reason}")
            image = renderer.generate_plate_special(plate, base, False, enhance=False)
            height, width = image.shape[:2]
            expected_width = 440 if color == "blue" else 480
            if (width, height) != (expected_width, 140):
                raise ValueError(f"unexpected plate dimensions: {width}x{height}")
            ok, encoded = cv2.imencode(".png", image)
            if not ok:
                raise ValueError(f"PNG encoding failed: {plate}")
            name = f"{color}-{index + 1:03d}.png"
            target = output / name
            data = encoded.tobytes()
            if target.exists() and target.read_bytes() != data:
                raise ValueError(f"existing output differs: {target}")
            if not target.exists():
                target.write_bytes(data)
            samples.append({
                "sample_id": f"reference-synthetic:{color}:{index + 1:03d}",
                "file": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "plate": plate,
                "display_plate": plate[:2] + "·" + plate[2:],
                "color": color,
                "physical_size_mm": [expected_width, 140],
                "width": width,
                "height": height,
                "bbox_xywh": [0, 0, width, height],
                "corners": [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
                "source_kind": "synthetic",
                "photo_type": "synthetic_plate",
                "split": None,
                "training_eligible": False,
                "pr2_eligible": False,
            })
    manifest = {
        "format_version": 1,
        "source": "https://github.com/Pengfei8324/chinese_license_plate_generator",
        "source_commit": commit,
        "license": "MIT; Copyright (c) 2020 leijd",
        "asset_sha256": {name: sha256(source / name) for name in (
            "LICENSE", "generate_multi_plate.py", "plate_model/blue_140.PNG",
            "plate_model/green_car_140.PNG")},
        "renderer": "MultiPlateGenerator.generate_plate_special",
        "seed": seed,
        "per_color": per_color,
        "limitations": [
            "Auxiliary synthetic plates only; no real photographs or PR-2 test samples",
            "No perspective, vehicle scene, occlusion, dirt or lighting augmentation",
            "Blue base contains the physical separator dot; green base contains its original logo",
        ],
        "samples": samples,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--per-color", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    manifest = generate(args.source, args.output, per_color=args.per_color, seed=args.seed)
    print(json.dumps({"samples": len(manifest["samples"]), "source_commit": SOURCE_COMMIT,
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
