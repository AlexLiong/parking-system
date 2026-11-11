"""生成合成车牌数据集（tools/gen_synthetic_plates.py）。

批量渲染蓝/绿牌并嵌入场景图（可加数据增强），写入 ``datasets/plates/{blue,green}/``，
同时生成 ``labels.csv``（文件名,车牌,颜色,增强级别）。用于：
- 识别流水线的离线评测（tools/eval_lpr.py）；
- 训练自训 CNN（tools/train_char_cnn.py）的原始素材。

**说明**：合成图用于验证流水线与产出可复现评测；真实照片识别请见 prompt 的
"导入车牌图片识别"功能（后端 REST 直接吃真实照片），CNN 路径可进一步提升真实照片鲁棒性。

运行示例：
    python tools/gen_synthetic_plates.py --count 200 --augment 0.6 --seed 42
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.config import get_config  # noqa: E402
from server.core.domain import PlateColor  # noqa: E402
from server.lpr.recognizer import imwrite_unicode  # noqa: E402
from server.lpr.render import PlateFonts, random_plate, render_scene  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成车牌数据集")
    ap.add_argument("--count", type=int, default=200, help="每种颜色的图片数量")
    ap.add_argument("--augment", type=float, default=0.6, help="增强强度 0~1")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（可复现）")
    ap.add_argument("--out", type=str, default="datasets/synthetic-plates", help="输出目录（默认 datasets/synthetic-plates；禁止覆盖 demo-lpr）")
    ap.add_argument("--scene", type=int, nargs=2, default=(900, 480), metavar=("W", "H"),
                    help="场景图尺寸")
    args = ap.parse_args()

    cfg = get_config()
    out_root = Path(args.out) if args.out else Path(cfg.paths.plates_dataset)
    if out_root.resolve() == Path(cfg.paths.plates_dataset).resolve():
        raise SystemExit("拒绝写入配置中的演示图库（datasets/demo-lpr）；请用 --out 指定独立输出目录")
    rng = random.Random(args.seed)
    fonts = PlateFonts()
    scene_size = (int(args.scene[0]), int(args.scene[1]))

    rows: list[tuple[str, str, str, float]] = []
    for color in (PlateColor.BLUE, PlateColor.GREEN):
        cdir = out_root / color.value
        cdir.mkdir(parents=True, exist_ok=True)
        for i in range(args.count):
            plate = random_plate(color, rng)
            # 一半干净、一半增强，兼顾基线与鲁棒性评测
            level = 0.0 if i % 2 == 0 else args.augment
            img = render_scene(plate, color, fonts, scene_size=scene_size,
                               augment_level=level, rng=rng)
            fname = f"{color.value}_{i:05d}.jpg"
            if imwrite_unicode(cdir / fname, img, ".jpg"):
                rows.append((f"{color.value}/{fname}", plate, color.value, level))
        print(f"  {color.value}: {args.count} 张 -> {cdir}")

    labels = out_root / "labels.csv"
    with open(labels, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "plate", "color", "augment_level"])
        w.writerows(rows)
    print(f"完成：共 {len(rows)} 张，标签写入 {labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
