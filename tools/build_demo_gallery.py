"""重新生成 datasets/demo-lpr 演示图库（18 张；蓝/小绿/大绿/黄）。

使用 server/lpr/render.py + 固定 MIT 字形/底板资产，无系统字体、无网络。
种子固定，文件名固定，可重复运行得到相同字节（PNG 编码确定）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from server.lpr import charset  # noqa: E402
from server.lpr.render import PlateFonts, render_scene  # noqa: E402

DEFAULT_SEED = 20261024
DEFAULT_OUTPUT = ROOT / "datasets/demo-lpr"
# 固定演示取景：个别合成底/字形组合 OCR 易把 D 看成 0；用显式合法车牌保证演示一致。
PLATE_OVERRIDES = {
    "green_large-000-clean.png": "鄂ABWTXHF",
    "green_large-001-clean.png": "琼BKBF39F",
    "green_large-002-clean.png": "贵ABYWFRF",
    "green_large-003-clean.png": "鄂JQ8PGQF",
}
# 分布固定：蓝 5、小绿 5、大绿 4、黄 4 = 18
PLAN = [
    ("blue", "blue-000-clean.png", 0),
    ("blue", "blue-001-clean.png", 1),
    ("blue", "blue-002-clean.png", 2),
    ("blue", "blue-003-clean.png", 3),
    ("blue", "blue-004-clean.png", 4),
    ("green", "green_car-000-clean.png", 5),
    ("green", "green_car-001-clean.png", 6),
    ("green", "green_car-002-clean.png", 7),
    ("green", "green_car-003-clean.png", 8),
    ("green", "green_car-004-clean.png", 9),
    ("green_large", "green_large-000-clean.png", 10),
    ("green_large", "green_large-001-clean.png", 11),
    ("green_large", "green_large-002-clean.png", 12),
    ("green_large", "green_large-003-clean.png", 13),
    ("yellow", "yellow-000-clean.png", 14),
    ("yellow", "yellow-001-clean.png", 15),
    ("yellow", "yellow-002-clean.png", 16),
    ("yellow", "yellow-003-clean.png", 17),
]


def generate(output: Path = DEFAULT_OUTPUT, seed: int = DEFAULT_SEED) -> dict:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # 清理旧生成图（保留 LICENSE/其他非 demo 文件）
    for pattern in ("blue-*-clean.png", "green_car-*-clean.png",
                    "green_truck-*-clean.png", "green_large-*-clean.png",
                    "yellow-*-clean.png"):
        for old in output.glob(pattern):
            old.unlink()
    fonts = PlateFonts()
    rng = random.Random(seed)
    used: set[str] = set()
    samples = []
    for plate_type, filename, index in PLAN:
        override = PLATE_OVERRIDES.get(filename)
        if override:
            plate = override
            ok, reason = charset.validate_plate_type(plate, plate_type)
            if not ok:
                raise ValueError(f"演示车牌覆盖非法 {plate}: {reason}")
        else:
            while True:
                plate = charset.generate_plate_type(plate_type, rng)
                if plate not in used:
                    used.add(plate)
                    break
        used.add(plate)
        image = render_scene(plate, plate_type, fonts,
                             scene_size=(960, 640), plate_scale=0.62,
                             augment_level=0.0,
                             rng=random.Random(seed + index))
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise RuntimeError(f"PNG 编码失败: {plate}")
        data = encoded.tobytes()
        target = output / filename
        target.write_bytes(data)
        spec = charset.plate_type_spec(plate_type)
        height, width = image.shape[:2]
        samples.append({
            "sample_id": f"{plate_type}-{index:03d}-clean",
            "file": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
            "plate": plate,
            "plate_type": plate_type,
            "style": "green_car" if plate_type == "green" else plate_type,
            "color": spec["color"],
            "vehicle_kind": spec["vehicle_kind"],
            "condition": "clean",
            "parameters": {"render": "server/lpr/render.py", "seed": seed,
                           "scene_size": [960, 640], "plate_scale": 0.62,
                           "augment_level": 0.0},
            "seed": seed,
            "width": width,
            "height": height,
            "bbox_xywh": [0, 0, width, height],
            "corners": [[0, 0], [width - 1, 0], [width - 1, height - 1],
                        [0, height - 1]],
            "source_kind": "synthetic",
            "purpose": "demo",
            "training_eligible": False,
            "pr2_eligible": False,
        })
    manifest = {
        "source": "server/lpr/render.py + MIT reference glyphs",
        "license": "MIT; reference glyphs assets from Pengfei8324/chinese_license_plate_generator",
        "assets_manifest": "server/lpr/models/reference-plates/manifest.json",
        "seed": seed,
        "purpose": "product demo, generated plate images (blue/green/green_large/yellow); not evaluation results",
        "format_version": 2,
        "samples": samples,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"samples": len(samples), "output": str(output), "seed": seed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    print(json.dumps(generate(args.output, args.seed), ensure_ascii=False))


if __name__ == "__main__":
    main()
