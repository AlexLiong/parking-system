"""车牌识别评测（tools/eval_lpr.py）。

两种模式：
1. ``--dataset DIR``：读取 ``DIR/labels.csv``（file,plate,color,augment_level）逐张评测；
2. 默认（在线合成）：按 ``--count/--augment/--seed`` 现场渲染场景图评测（可复现）。

输出指标（供"测试结果说明"文档引用）：
- 整牌完全正确率（top-1 exact match）；
- 字符级准确率；
- 自动放行正确率 / 转人工率（鲁棒性语义：不确定不放行）；
- 蓝/绿分组、干净/增强分组；平均单张耗时。

运行示例：
    python tools/eval_lpr.py --count 60 --augment 0.6 --seed 42
    python tools/eval_lpr.py --dataset datasets/external/style-benchmark-v2
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.config import get_config  # noqa: E402
from server.core.domain import PlateColor, PlateContext  # noqa: E402
from server.lpr.recognizer import PlateRecognizer, imread_unicode  # noqa: E402
from server.lpr.render import PlateFonts, random_plate, render_scene  # noqa: E402


class Metrics:
    def __init__(self):
        self.total = 0
        self.exact = 0          # 整牌完全正确（不论是否自动放行）
        self.auto_ok = 0        # 自动放行且正确
        self.auto_bad = 0       # 自动放行但错误（危险：应为 0 或极少）
        self.manual = 0         # 转人工（含正确但保守、错误但拦截）
        self.manual_correct = 0
        self.char_ok = 0
        self.char_total = 0
        self.ms_sum = 0.0

    def add(self, truth, got, success, ms):
        self.total += 1
        self.ms_sum += ms
        ok = (got == truth)
        if ok:
            self.exact += 1
        for a, b in zip(truth, got or ""):
            self.char_total += 1
            if a == b:
                self.char_ok += 1
        if success:
            if ok:
                self.auto_ok += 1
            else:
                self.auto_bad += 1
        else:
            self.manual += 1
            if ok:
                self.manual_correct += 1

    def report(self, title):
        n = max(1, self.total)
        print(f"\n=== {title} ===")
        print(f"样本数            : {self.total}")
        print(f"整牌完全正确率     : {100.0 * self.exact / n:.1f}%  ({self.exact}/{self.total})")
        print(f"字符级准确率       : {100.0 * self.char_ok / max(1, self.char_total):.1f}%  "
              f"({self.char_ok}/{self.char_total})")
        print(f"自动放行且正确     : {100.0 * self.auto_ok / n:.1f}%  ({self.auto_ok}/{self.total})")
        print(f"自动放行但错误(危险): {100.0 * self.auto_bad / n:.1f}%  ({self.auto_bad}/{self.total})")
        print(f"转人工确认         : {100.0 * self.manual / n:.1f}%  ({self.manual}/{self.total})"
              f"，其中其实正确 {self.manual_correct}")
        print(f"平均单张耗时       : {self.ms_sum / n:.0f} ms")


def eval_images(rec, items, fonts=None):
    """items: list of (image_bgr_or_path, truth_plate)."""
    m = Metrics()
    for img_or_path, truth in items:
        t0 = time.perf_counter()
        if isinstance(img_or_path, (str, Path)):
            r = rec.recognize(str(img_or_path), context=PlateContext())
        else:
            r = rec.recognize_bgr(img_or_path, context=PlateContext())
        ms = (time.perf_counter() - t0) * 1000
        m.add(truth, r.plate or "", r.success, ms)
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="车牌识别评测")
    ap.add_argument("--dataset", type=str, default=None, help="数据集目录（含 labels.csv）")
    ap.add_argument("--count", type=int, default=60, help="在线合成时每颜色样本数")
    ap.add_argument("--augment", type=float, default=0.6, help="在线合成增强强度")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scene", type=int, nargs=2, default=(900, 480), metavar=("W", "H"))
    args = ap.parse_args()

    cfg = get_config()
    rec = PlateRecognizer(cfg.lpr)
    print(f"分类器后端可用: {getattr(rec.classifier, 'available', True)}  "
          f"char_size={tuple(cfg.lpr.char_size)}  conf_threshold={cfg.lpr.conf_threshold}")

    if args.dataset:
        d = Path(args.dataset)
        labels = d / "labels.csv"
        items = []
        with open(labels, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                p = d / row["file"]
                if p.exists():
                    items.append((str(p), row["plate"]))
        m = eval_images(rec, items)
        m.report(f"数据集评测 {d}")
        return 0

    fonts = PlateFonts()
    rng = random.Random(args.seed)
    overall = Metrics()
    for color in (PlateColor.BLUE, PlateColor.GREEN):
        sub = Metrics()
        items = []
        for i in range(args.count):
            plate = random_plate(color, rng)
            level = 0.0 if i % 2 == 0 else args.augment
            img = render_scene(plate, color, fonts, scene_size=tuple(args.scene),
                               augment_level=level, rng=rng)
            items.append((img, plate))
        m = eval_images(rec, items)
        m.report(f"{color.value}（干净+增强各半）")
        # 合并
        overall.total += m.total
        overall.exact += m.exact
        overall.auto_ok += m.auto_ok
        overall.auto_bad += m.auto_bad
        overall.manual += m.manual
        overall.manual_correct += m.manual_correct
        overall.char_ok += m.char_ok
        overall.char_total += m.char_total
        overall.ms_sum += m.ms_sum
    overall.report("合计")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
