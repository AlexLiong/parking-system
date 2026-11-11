"""车牌识别模块（lpr）。

流水线：预处理 → 定位 → 校正 → 颜色判定 → 分割 → 字符识别 → 校验 →（多帧）融合。
对外门面 ``LPRService``；纯 CV 流水线 ``PlateRecognizer``（可单独测试/替换）。
"""
from .recognizer import PlateRecognizer, imread_unicode, imwrite_unicode
from .service import LPRService
from .classifier import (TemplateClassifier, OnnxClassifier, build_classifier,
                         HEAD_HANZI, HEAD_LETTER, HEAD_ALNUM)
from .fuser import MultiFrameFuser
from .validator import PlateValidator

__all__ = [
    "PlateRecognizer", "LPRService", "TemplateClassifier", "OnnxClassifier",
    "build_classifier", "MultiFrameFuser", "PlateValidator",
    "imread_unicode", "imwrite_unicode",
    "HEAD_HANZI", "HEAD_LETTER", "HEAD_ALNUM",
]
