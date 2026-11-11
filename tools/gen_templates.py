"""生成字符模板（tools/gen_templates.py）。

用号牌规范字体把 31 省份汉字 / 24 发牌机关字母 / 34 序号字母数字渲染为归一化模板，
写入 ``server/lpr/models/templates/``（PNG + meta.json），供 ``TemplateClassifier`` 使用。

跨平台：文件名用索引（``hanzi_000.png`` 等）而非汉字，避免文件系统编码问题；
字符↔文件的映射保存在 meta.json。运行：``python tools/gen_templates.py``。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402

from server.core.config import get_config  # noqa: E402
from server.lpr.classifier import HEAD_CHARS  # noqa: E402
from server.lpr.render import PlateFonts, render_char_template  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="生成车牌字符模板")
    ap.add_argument("--out", type=str, default=None,
                    help="输出目录（默认取 config.lpr.template_dir）")
    args = ap.parse_args()

    cfg = get_config()
    out_dir = Path(args.out) if args.out else Path(cfg.lpr.template_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    char_size = tuple(cfg.lpr.char_size)

    fonts = PlateFonts()
    print(f"CJK 字体: {fonts.cjk_path}")
    print(f"拉丁字体: {fonts.latin_path}")
    print(f"输出目录: {out_dir}  字符规格: {char_size}")

    heads: dict[str, list[str]] = {}
    files: dict[str, list[str]] = {}
    total = 0
    for head, chars in HEAD_CHARS.items():
        heads[head] = list(chars)
        files[head] = []
        for i, ch in enumerate(chars):
            tmpl = render_char_template(ch, fonts, char_size)
            fn = f"{head}_{i:03d}.png"
            ok = cv2.imwrite(str(out_dir / fn), tmpl)
            if not ok:
                print(f"  [跳过] 无法渲染 {head} '{ch}'（字体缺字形？）")
                # 仍占位以保持与 chars 的一一对应；分类器 load 时会跳过读不到的文件
                files[head].append(fn)
                continue
            files[head].append(fn)
            total += 1
        print(f"  {head}: {len(chars)} 类")

    meta = {
        "char_size": list(char_size),
        "heads": heads,
        "files": files,
        "cjk_font": fonts.cjk_path,
        "latin_font": fonts.latin_path,
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成：共 {total} 张模板，meta.json 已写入 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
