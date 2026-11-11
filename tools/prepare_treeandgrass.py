"""Extract and inventory the treeandgrass license-plate archive without trusting labels."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://github.com/treeandgrass/License-plate-data-set"
SOURCE_COMMIT = "dec642ea4471b8f22347c4ee13a52fb1a212109f"
ARCHIVE_SHA256 = "210bf81985bad271e5d8a8132bce742b9243ed93ae9174cf80f7a5ca8d42d4b1"
LICENSE_SHA256 = "83e4dd21429a91fb7cea67a476032a9641425e5355df2e0f589a738b6ec9fd2c"
PREFIX = "Chineselicenseplatepicture"


def prepare(archive: Path, output_dir: Path) -> dict:
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("archive SHA-256 differs from the reviewed download")
    license_path = output_dir.parent / "LICENSE"
    if not license_path.is_file() or hashlib.sha256(license_path.read_bytes()).hexdigest() != LICENSE_SHA256:
        raise ValueError("source MIT license missing or changed")
    output_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    excluded = []
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != PREFIX:
                raise ValueError(f"unexpected archive path: {entry.filename!r}")
            if entry.is_dir():
                continue
            if len(path.parts) != 2 or path.suffix.lower() not in (".jpg", ".jpeg"):
                raise ValueError(f"unexpected archive member: {entry.filename!r}")
            data = bundle.read(entry)
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                excluded.append({"file": entry.filename, "reason": "image_decode_failed",
                                 "sha256": hashlib.sha256(data).hexdigest()})
                continue
            dest = output_dir / path.name
            if dest.exists() and dest.read_bytes() != data:
                raise ValueError(f"existing image differs: {dest}")
            if not dest.exists():
                dest.write_bytes(data)
            height, width = image.shape[:2]
            samples.append({
                "sample_id": f"treeandgrass:{path.name}",
                "file": f"{PREFIX}/{path.name}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "width": width,
                "height": height,
                "filename_label": path.stem,
                "plate": None,
                "bbox_xywh": None,
                "corners": None,
                "photo_type": "unreviewed",
                "condition": "unreviewed",
                "annotation_status": "filename_unverified",
                "source_kind": "photo_provenance_unverified",
                "split": None,
            })
    hashes = Counter(sample["sha256"] for sample in samples)
    return {
        "format_version": 1,
        "source": SOURCE_URL,
        "source_commit": SOURCE_COMMIT,
        "repository_license": "MIT",
        "license_sha256": LICENSE_SHA256,
        "archive_sha256": ARCHIVE_SHA256,
        "samples": samples,
        "excluded": excluded,
        "summary": {
            "images": len(samples),
            "excluded": len(excluded),
            "unique_image_hashes": len(hashes),
            "duplicate_image_files": sum(count - 1 for count in hashes.values()),
            "dimensions": dict(Counter(f'{s["width"]}x{s["height"]}' for s in samples).most_common(20)),
        },
        "limitations": [
            "Repository does not document each photograph's origin",
            "Filename labels and close-up/vehicle classes require manual review",
            "No source bounding boxes or four-corner annotations",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path,
                        default=PROJECT_ROOT / "datasets/external/treeandgrass-license-plates.zip")
    parser.add_argument("--output-dir", type=Path,
                        default=PROJECT_ROOT / "datasets/external/treeandgrass/Chineselicenseplatepicture")
    parser.add_argument("--manifest", type=Path,
                        default=PROJECT_ROOT / "datasets/real/treeandgrass-manifest.json")
    args = parser.parse_args()
    manifest = prepare(args.archive, args.output_dir)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    print(json.dumps(manifest["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
