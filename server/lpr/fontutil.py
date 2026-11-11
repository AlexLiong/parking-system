"""字体解析（lpr/fontutil.py）。

用于**离线生成**字符模板与合成车牌图（tools/）。运行时识别使用已生成的模板，
不依赖字体。PDF 报表另用 ReportLab 内置 CID 中文字体，也不需要外部字体文件。

跨平台策略：优先用仓库内置字体（assets/fonts/），否则按 OS 搜索常见系统中文字体；
拉丁粗体优先用 matplotlib 自带的 DejaVuSans-Bold（自由许可、随依赖安装、跨平台一致）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("lpr.fontutil")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_FONT_DIR = _PROJECT_ROOT / "assets" / "fonts"

# 常见中文字体候选（按平台）
_CJK_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # HarmonyOS(PC) 常见路径
    "/system/fonts/NotoSansCJK-Regular.ttc",
]


def _bundled(patterns: list[str]) -> Optional[Path]:
    if not _BUNDLED_FONT_DIR.exists():
        return None
    for pat in patterns:
        for p in _BUNDLED_FONT_DIR.glob(pat):
            return p
    return None


def resolve_cjk_font() -> Optional[str]:
    """返回一个含中文字形的字体文件路径（.ttf/.ttc/.otf），找不到返回 None。"""
    b = _bundled(["*CJK*", "*Hei*", "*Song*", "*PingFang*", "*wqy*", "*yahei*", "*.ttc", "*.ttf", "*.otf"])
    if b:
        return str(b)
    for cand in _CJK_CANDIDATES:
        if Path(cand).exists():
            return cand
    log.warning("未找到可用中文字体，模板/合成图可能缺少汉字字形")
    return None


def resolve_latin_bold_font() -> Optional[str]:
    """返回拉丁粗体字体（用于字母/数字），优先 matplotlib 自带 DejaVuSans-Bold。"""
    try:
        import matplotlib
        p = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans-Bold.ttf"
        if p.exists():
            return str(p)
    except Exception:  # noqa: BLE001
        pass
    for cand in ("C:/Windows/Fonts/arialbd.ttf",
                 "/Library/Fonts/Arial Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(cand).exists():
            return cand
    return resolve_cjk_font()


if __name__ == "__main__":  # 便于排查环境字体
    print("platform:", sys.platform)
    print("cjk:", resolve_cjk_font())
    print("latin_bold:", resolve_latin_bold_font())
