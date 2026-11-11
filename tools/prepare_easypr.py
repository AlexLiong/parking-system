"""Build a provenance-preserving manifest from EasyPR's GDTS real photos."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.lpr.charset import validate_grammar  # noqa: E402

SOURCE_URL = "https://github.com/liuruoze/EasyPR"
SOURCE_COMMIT = "a0a9f3c3f612e7c8d9046bd9c5770249cfb86850"
SOURCE_LICENSE = "GDSL-0.1"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def parse_annotations(xml_path: Path) -> dict[str, list[dict]]:
    # EasyPR's XML declaration says ISO-8859-1, but the file bytes are UTF-8.
    root = ET.fromstring(xml_path.read_bytes().decode("utf-8"))
    annotations: dict[str, list[dict]] = {}
    for image in root.findall("image"):
        name = image.findtext("imageName")
        if not name:
            raise ValueError("missing imageName")
        rectangles = []
        for rect in image.findall("taggedRectangles/taggedRectangle"):
            label = (rect.text or "").strip()
            if ":" not in label:
                continue
            color_name, plate = label.split(":", 1)
            rectangles.append({
                "plate": plate.strip(),
                "color": "blue" if color_name == "蓝牌" else color_name,
                "bbox_xywh": [int(rect.attrib[k]) for k in ("x", "y", "width", "height")],
                "rotation_deg": float(rect.attrib.get("rotation", 0)),
                "locate_type": rect.attrib.get("locateType"),
            })
        if name in annotations and annotations[name] != rectangles:
            raise ValueError(f"conflicting imageName annotations: {name!r}")
        annotations[name] = rectangles
    return annotations


def build_manifest(source: Path) -> dict:
    image_dir = source / "resources" / "image" / "general_test"
    license_path = source / "resources" / "image" / "GDSL.txt"
    if not license_path.is_file():
        raise FileNotFoundError(f"GDSL license missing: {license_path}")
    annotations = parse_annotations(image_dir / "GroundTruth_others.xml")
    images = sorted((p for p in image_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES),
                    key=lambda p: p.name)
    samples = []
    excluded = []
    for path in images:
        matches = [r for r in annotations.get(path.stem, []) if r["plate"] == path.stem]
        if len(matches) != 1:
            excluded.append({"file": path.name, "reason": "missing_or_ambiguous_primary_bbox"})
            continue
        primary = matches[0]
        valid, reason = validate_grammar(path.stem, primary["color"])
        if not valid:
            excluded.append({"file": path.name, "reason": f"unsupported_plate: {reason}"})
            continue
        data = path.read_bytes()
        if not data:
            excluded.append({"file": path.name, "reason": "empty_image"})
            continue
        bucket = int.from_bytes(hashlib.sha256(path.stem.encode("utf-8")).digest()[:4], "big") % 100
        split = "train" if bucket < 70 else ("val" if bucket < 85 else "test")
        samples.append({
            "sample_id": f"easypr:{path.stem}",
            "file": path.relative_to(source).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "plate": path.stem,
            "color": primary["color"],
            "bbox_xywh": primary["bbox_xywh"],
            "rotation_deg": primary["rotation_deg"],
            "corners": None,
            "corners_status": "not_provided_by_source",
            "photo_type": "unreviewed",
            "condition": "unreviewed",
            "split": split,
            "source_kind": "real_photo_source",
        })
    return {
        "format_version": 1,
        "source": SOURCE_URL,
        "source_commit": SOURCE_COMMIT,
        "license": SOURCE_LICENSE,
        "license_sha256": hashlib.sha256(license_path.read_bytes()).hexdigest(),
        "annotation_file": "resources/image/general_test/GroundTruth_others.xml",
        "annotation_sha256": hashlib.sha256((image_dir / "GroundTruth_others.xml").read_bytes()).hexdigest(),
        "split_rule": "sha256(plate UTF-8) first 32 bits mod 100: train<70, val<85, test otherwise",
        "samples": samples,
        "excluded": excluded,
        "annotations_without_image": sorted(set(annotations) - {p.stem for p in images}),
        "limitations": ["No source four-corner labels", "Photo type and condition require review",
                        "No green-plate coverage established"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=PROJECT_ROOT / "datasets/external/EasyPR")
    parser.add_argument("--out", type=Path,
                        default=PROJECT_ROOT / "datasets/real/easypr-manifest.json")
    args = parser.parse_args()
    manifest = build_manifest(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {split: sum(s["split"] == split for s in manifest["samples"])
              for split in ("train", "val", "test")}
    print(json.dumps({"out": str(args.out), "counts": counts,
                      "excluded": len(manifest["excluded"]),
                      "annotations_without_image": manifest["annotations_without_image"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
