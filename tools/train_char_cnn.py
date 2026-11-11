"""自训字符识别 CNN（tools/train_char_cnn.py）——课程"神经网络"方法（可选路径）。

产出一个 ``char_cnn.onnx``，被 ``server/lpr/classifier.py::OnnxClassifier`` 加载后
取代默认的模板匹配后端（``config.lpr.backend: auto`` 时自动优先用 onnx）。

**I/O 契约**（必须与 OnnxClassifier 一致，否则回退模板匹配）：
- 输入：``float32`` 张量 ``[N, 1, H, W]``，单通道，像素已 ``/255``；
  ``(W, H) = config.lpr.char_size``（cv2 语义：宽在前）；
- 输出：3 个头，顺序 ``[hanzi(31省汉字), letter(24发牌字母), alnum(34序号)]``，
  每个头输出对应类别的 **logits**（softmax 由 OnnxClassifier 内部完成）。

训练数据由 ``server/lpr/render.py`` 的号牌规范字体离线渲染 + 数据增强生成（RR-1.1），
**训练/推理与 3D 建模、运行时识别流水线解耦**（prompt / RR-4.2）。

依赖：``torch``（可选依赖，见 requirements.txt 末尾注释）。推理端只需 ``onnxruntime``。
    pip install torch            # CPU 版即可
    python tools/train_char_cnn.py --epochs 12 --per-class 400
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402

from server.core.config import load_config  # noqa: E402
from server.core.domain import PlateColor  # noqa: E402
from server.lpr.classifier import HEAD_ALNUM, HEAD_CHARS, HEAD_HANZI, HEAD_LETTER  # noqa: E402
from server.lpr.render import PlateFonts, render_char_template  # noqa: E402

HEAD_ORDER = [HEAD_HANZI, HEAD_LETTER, HEAD_ALNUM]


def _log(msg: str) -> None:
    print(f"[train] {msg}", flush=True)


# --------------------------------------------------------------------------
# 数据生成（numpy/cv2；无需 torch）
# --------------------------------------------------------------------------
def _augment_char(block: np.ndarray, rng: random.Random) -> np.ndarray:
    """对归一化字符块做轻量增强：平移/旋转/缩放/亮度/噪声/形变。"""
    h, w = block.shape[:2]
    out = block.astype(np.float32)

    # 轻微仿射（平移 + 小角度旋转 + 缩放）
    if rng.random() < 0.85:
        ang = rng.uniform(-8, 8)
        sc = rng.uniform(0.9, 1.1)
        tx = rng.uniform(-0.06, 0.06) * w
        ty = rng.uniform(-0.06, 0.06) * h
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), ang, sc)
        M[0, 2] += tx
        M[1, 2] += ty
        out = cv2.warpAffine(out, M, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    # 亮度/对比度
    if rng.random() < 0.6:
        out = out * rng.uniform(0.75, 1.25) + rng.uniform(-25, 25)

    # 笔画粗细（腐蚀/膨胀）
    if rng.random() < 0.3:
        k = np.ones((2, 2), np.uint8)
        u8 = np.clip(out, 0, 255).astype(np.uint8)
        u8 = cv2.erode(u8, k) if rng.random() < 0.5 else cv2.dilate(u8, k)
        out = u8.astype(np.float32)

    # 高斯噪声
    if rng.random() < 0.5:
        out = out + np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, 8, out.shape)

    return np.clip(out, 0, 255).astype(np.uint8)


def build_dataset(char_size: tuple[int, int], per_class: int, seed: int):
    """为 3 个头分别构建 (X, y)。返回 {head: (images[N,H,W] uint8, labels[N] int)}。"""
    rng = random.Random(seed)
    fonts = PlateFonts()
    w, h = int(char_size[0]), int(char_size[1])
    data: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for head in HEAD_ORDER:
        chars = HEAD_CHARS[head]
        imgs: list[np.ndarray] = []
        labels: list[int] = []
        for ci, ch in enumerate(chars):
            color = PlateColor.BLUE if rng.random() < 0.5 else PlateColor.GREEN
            base = render_char_template(ch, fonts, (w, h), color)
            if base.shape[1] != w or base.shape[0] != h:
                base = cv2.resize(base, (w, h), interpolation=cv2.INTER_AREA)
            for _ in range(per_class):
                c = PlateColor.BLUE if rng.random() < 0.5 else PlateColor.GREEN
                variant = _augment_char(base, rng) if rng.random() < 0.9 else base
                # 少量以另一色底重渲染，覆盖蓝(白字)/绿(黑字)两种极性
                if rng.random() < 0.25:
                    variant = render_char_template(ch, fonts, (w, h), c)
                    if variant.shape[1] != w or variant.shape[0] != h:
                        variant = cv2.resize(variant, (w, h), interpolation=cv2.INTER_AREA)
                imgs.append(variant)
                labels.append(ci)
        data[head] = (np.asarray(imgs, dtype=np.uint8), np.asarray(labels, dtype=np.int64))
        _log(f"  头 {head}: {len(chars)} 类 × {per_class} = {len(imgs)} 样本")
    return data


# --------------------------------------------------------------------------
# 训练（torch）
# --------------------------------------------------------------------------
def train(data, char_size, *, epochs, batch_size, lr, out_path, seed) -> None:
    try:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F  # noqa: N812
    except ImportError:
        raise SystemExit(
            "[train] 未安装 torch。训练 CNN 为可选路径：\n"
            "    pip install torch        # CPU 版即可\n"
            "默认后端为模板匹配（tools/gen_templates.py），无需 torch 即可运行系统。")

    torch.manual_seed(seed)
    w, h = int(char_size[0]), int(char_size[1])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _log(f"训练设备：{device}  torch {torch.__version__}")

    class CharCNN(nn.Module):
        """轻量 CNN 主干 + 3 分类头（共享特征，按位置头输出）。"""

        def __init__(self, n_hanzi, n_letter, n_alnum):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
                nn.MaxPool2d(2),
                nn.Conv2d(64, 128, 3, padding=1), nn.BatchNorm2d(128), nn.ReLU(),
                nn.AdaptiveAvgPool2d((2, 2)),
            )
            flat = 128 * 2 * 2
            self.heads = nn.ModuleList([
                nn.Sequential(nn.Linear(flat, 128), nn.ReLU(), nn.Dropout(0.3),
                              nn.Linear(128, n))
                for n in (n_hanzi, n_letter, n_alnum)
            ])

        def forward(self, x):
            x = self.features(x)
            x = torch.flatten(x, 1)
            return [head(x) for head in self.heads]

    model = CharCNN(len(HEAD_CHARS[HEAD_HANZI]), len(HEAD_CHARS[HEAD_LETTER]),
                    len(HEAD_CHARS[HEAD_ALNUM])).to(device)

    # 预转为张量（灰度 /255，形状 [N,1,H,W]）
    tensors = {}
    for i, head in enumerate(HEAD_ORDER):
        imgs, labels = data[head]
        X = torch.from_numpy(imgs.astype(np.float32) / 255.0).unsqueeze(1)  # [N,1,H,W]
        y = torch.from_numpy(labels)
        perm = torch.randperm(X.shape[0])
        X, y = X[perm], y[perm]
        n_val = max(1, int(0.15 * X.shape[0]))
        tensors[head] = {
            "Xtr": X[n_val:].to(device), "ytr": y[n_val:].to(device),
            "Xva": X[:n_val].to(device), "yva": y[:n_val].to(device),
        }

    params = list(model.parameters())
    opt = torch.optim.Adam(params, lr=lr)
    head_idx = {head: i for i, head in enumerate(HEAD_ORDER)}

    for ep in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        steps = 0
        for head in HEAD_ORDER:
            t = tensors[head]
            hi = head_idx[head]
            n = t["Xtr"].shape[0]
            idx = torch.randperm(n, device=device)
            for b in range(0, n, batch_size):
                sel = idx[b:b + batch_size]
                xb, yb = t["Xtr"][sel], t["ytr"][sel]
                outs = model(xb)
                loss = F.cross_entropy(outs[hi], yb)
                opt.zero_grad()
                loss.backward()
                opt.step()
                total_loss += float(loss.item())
                steps += 1
        # 验证
        model.eval()
        accs = []
        with torch.no_grad():
            for head in HEAD_ORDER:
                t = tensors[head]
                outs = model(t["Xva"])
                pred = outs[head_idx[head]].argmax(1)
                accs.append(float((pred == t["yva"]).float().mean().item()))
        _log(f"epoch {ep:>3}/{epochs}  loss={total_loss / max(1, steps):.4f}  "
             f"val_acc hanzi/letter/alnum = "
             f"{accs[0]:.3f}/{accs[1]:.3f}/{accs[2]:.3f}")

    # 导出 ONNX（动态 batch；输出顺序 = HEAD_ORDER）
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    dummy = torch.zeros(1, 1, h, w, device=device)
    torch.onnx.export(
        model, dummy, str(out_path),
        input_names=["input"], output_names=HEAD_ORDER,
        dynamic_axes={"input": {0: "batch"}, **{o: {0: "batch"} for o in HEAD_ORDER}},
        opset_version=13, do_constant_folding=True)
    _log(f"已导出 ONNX：{out_path}")
    _log("提示：将 config.yaml 的 lpr.backend 设为 auto/onnx 即启用该模型（缺失自动回退模板匹配）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="训练字符识别 CNN 并导出 ONNX")
    ap.add_argument("--per-class", type=int, default=400, help="每类样本数（含增强）")
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default=None,
                    help="输出 onnx 路径（默认 config.lpr.onnx_model）")
    args = ap.parse_args()

    cfg = load_config()
    char_size = tuple(cfg.lpr.char_size)
    out_path = args.out or str(cfg.lpr.onnx_model)
    _log(f"char_size(WxH)={char_size}  输出={out_path}")

    data = build_dataset(char_size, args.per_class, args.seed)
    train(data, char_size, epochs=args.epochs, batch_size=args.batch_size,
          lr=args.lr, out_path=out_path, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
