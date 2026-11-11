"""Evaluate the current end-to-end LPR on a provenance-checked photo manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.config import get_config  # noqa: E402
from server.lpr.recognizer import PlateRecognizer  # noqa: E402
from server.lpr.hyperlpr_adapter import HyperPlateRecognizer  # noqa: E402


def evaluate(manifest: dict, data_root: Path, split: str, recognizer) -> dict:
    if manifest.get("format_version") != 1 or not manifest.get("license"):
        raise ValueError("unsupported manifest or missing source license")
    selected = [s for s in manifest["samples"] if s["split"] == split]
    if not selected:
        raise ValueError(f"no samples in {split!r} split")
    rows = []
    for sample in selected:
        if sample.get("source_kind") != "real_photo_source":
            raise ValueError(f"non-photo sample in real evaluation: {sample['sample_id']}")
        path = data_root / sample["file"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != sample["sha256"]:
            raise ValueError(f"image hash mismatch: {sample['sample_id']}")
        start = time.perf_counter()
        result = recognizer.recognize(str(path))
        truth, got = sample["plate"], result.plate or ""
        rows.append({
            "sample_id": sample["sample_id"], "sha256": digest,
            "truth": truth, "prediction": got, "exact": got == truth,
            "char_correct": sum(a == b for a, b in zip(truth, got)),
            "char_total": len(truth), "auto_pass": bool(result.success),
            "wrong_auto_pass": bool(result.success and got != truth),
            "manual": bool(result.need_manual), "message": result.message,
            "confidence": result.overall_conf,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
            "photo_type": sample["photo_type"], "condition": sample["condition"],
        })
    count = len(rows)
    exact = sum(r["exact"] for r in rows)
    char_total = sum(r["char_total"] for r in rows)
    return {
        "source": manifest["source"], "source_commit": manifest["source_commit"],
        "license": manifest["license"], "split": split,
        "model_id": getattr(recognizer, "model_id", "legacy-template"),
        "metrics": {"samples": count, "exact": exact, "exact_rate": exact / count,
                    "char_correct": sum(r["char_correct"] for r in rows),
                    "char_total": char_total,
                    "char_accuracy": sum(r["char_correct"] for r in rows) / char_total,
                    "wrong_auto_pass": sum(r["wrong_auto_pass"] for r in rows),
                    "manual": sum(r["manual"] for r in rows)},
        "pr2_eligible": False,
        "pr2_ineligible_reason": "No reviewed clear/dirty, closeup/vehicle and green-plate strata or four-corner labels",
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=PROJECT_ROOT / "datasets/real/easypr-manifest.json")
    parser.add_argument("--data-root", type=Path,
                        default=PROJECT_ROOT / "datasets/external/EasyPR")
    parser.add_argument("--split", choices=("train", "val", "test"), default="test")
    parser.add_argument("--backend", choices=("template", "hyperlpr"), default="template")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rec = (HyperPlateRecognizer(get_config().lpr) if args.backend == "hyperlpr"
           else PlateRecognizer(get_config().lpr))
    report = evaluate(json.loads(args.manifest.read_text(encoding="utf-8")),
                      args.data_root, args.split, rec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
