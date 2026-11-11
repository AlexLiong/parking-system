"""车牌识别服务门面（lpr/service.py）。

``LPRService`` 是 AccessControl 调用的入口（设计 §3.2）：在纯 CV 流水线
``PlateRecognizer`` 之上，负责注入业务上下文（在场车牌用于易混仲裁）、写识别日志、
多帧融合。识别本身与数据库解耦：db/repo 为可选注入，缺省时退化为纯识别。
"""
from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Optional

import cv2

from ..core.config import LPRConfig
from ..core.domain import PlateContext, RecognitionResult
from .charset import validate_grammar
from .fuser import MultiFrameFuser
from .pipeline import build_variants, is_low_result, pick_best
from .recognizer import PlateRecognizer, imread_unicode

log = logging.getLogger("lpr.service")


class LPRService:
    def __init__(self, recognizer: PlateRecognizer, cfg: LPRConfig,
                 *, record_repo=None, log_repo=None, list_repo=None):
        self.recognizer = recognizer
        self.cfg = cfg
        self.records = record_repo     # ParkingRecordRepo（取在场车牌做仲裁）
        self.logs = log_repo           # RecognitionLogRepo（写识别日志）
        self.lists = list_repo         # VehicleListRepo（白名单校验，可选）
        self.fuser = MultiFrameFuser(cfg.conf_threshold)

    # ----------------------------- 上下文 -----------------------------
    def _build_context(self, gate: str, context: Optional[PlateContext]) -> PlateContext:
        if context is not None:
            return context
        ctx = PlateContext(gate=gate)
        if self.records is not None:
            try:
                ctx.in_lot_plates = self.records.open_plates()
            except Exception:  # noqa: BLE001
                log.exception("取在场车牌失败，仲裁上下文降级为空")
        return ctx

    def _log(self, gate: str, image_path: Optional[str], r: RecognitionResult) -> None:
        if self.logs is None:
            return
        try:
            self.logs.add(
                gate=gate, image_path=image_path, plate=r.plate,
                plate_color=r.color.value if r.color else None,
                char_confs=r.char_confs, overall_conf=r.overall_conf,
                elapsed_ms=r.elapsed_ms, success=r.success,
            )
        except Exception:  # noqa: BLE001
            log.exception("写识别日志失败")

    # ----------------------------- 对外入口 -----------------------------
    def recognize(self, image_path: str, *, gate: str = "in",
                  context: Optional[PlateContext] = None,
                  crop_dir: Optional[Path] = None,
                  commit=None) -> RecognitionResult:
        """识别单张车牌图像。返回 RecognitionResult（不抛异常，失败以 need_manual 标记）。"""
        image = imread_unicode(image_path)
        if image is None:
            result = RecognitionResult(need_manual=True, source_status="unreadable",
                                       message="无法读取图像")
            self._log(gate, image_path, result)
            if commit is not None:
                commit()
            return result
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        mean, std = cv2.meanStdDev(gray)
        mean_luma, std_luma = float(mean[0, 0]), float(std[0, 0])
        metrics = {
            "mean": round(mean_luma, 2),
            "std": round(std_luma, 2),
            "laplacian_var": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2),
        }
        if std_luma < 5 and (mean_luma < 10 or mean_luma > 245):
            result = RecognitionResult(need_manual=True, source_status="blank",
                                       image_metrics=metrics, message="图像黑屏或白屏")
            self._log(gate, image_path, result)
            if commit is not None:
                commit()
            return result
        ctx = self._build_context(gate, context)
        result = self.recognizer.recognize(image_path, context=ctx, crop_dir=crop_dir,
                                           tag=gate)
        # 统一识别管线：原图低置信/无结果时尝试简单变体（CLAHE/2x/旋转/bbox 裁剪），
        # 取置信度最高结果；HyperLPR3 仍为主，不引入 PR-2 严格校验。
        if (getattr(self.cfg, "pipeline_variants", False)
                and is_low_result(result, self.cfg.conf_threshold)
                and hasattr(self.recognizer, "recognize_bgr")):
            try:
                variants = build_variants(
                    image, self.recognizer,
                    max_variants=int(getattr(self.cfg, "pipeline_max_variants", 6)),
                    max_side=int(getattr(self.cfg, "max_image_side", 1280)))
            except Exception:  # noqa: BLE001 - 变体生成失败不影响原图结果
                log.exception("识别变体生成失败")
                variants = []
            results = [result]
            for index, variant in enumerate(variants):
                try:
                    candidate = self.recognizer.recognize_bgr(
                        variant, context=ctx, crop_dir=crop_dir,
                        tag=f"{gate}_v{index}")
                except TypeError:
                    try:
                        candidate = self.recognizer.recognize_bgr(variant)
                    except Exception:  # noqa: BLE001
                        log.exception("识别变体执行失败: %s", index)
                        continue
                except Exception:  # noqa: BLE001
                    log.exception("识别变体执行失败: %s", index)
                    continue
                if candidate is not None:
                    results.append(candidate)
            best = pick_best(results)
            if best is not None:
                result = best
        result.image_metrics = metrics
        self._log(gate, image_path, result)
        if commit is not None:
            commit()
        return result

    def recognize_frames(self, image_paths: list[str], *, gate: str = "in",
                         crop_dir: Optional[Path] = None,
                         frame_results: Optional[list[RecognitionResult]] = None) -> RecognitionResult:
        """视频/连拍多帧识别 + 投票融合（FR-10.5 / RR-1.3）。"""
        if not 2 <= len(image_paths) <= 5:
            raise ValueError("多帧识别需要 2 至 5 张图像")
        fingerprints = []
        for path in image_paths:
            try:
                fingerprints.append(hashlib.sha256(Path(path).read_bytes()).digest())
            except OSError:
                fingerprints.append(None)
        distinct_frames = (None not in fingerprints and
                           len(set(fingerprints)) == len(fingerprints))
        results = []
        for path in image_paths:
            try:
                result = self.recognize(path, gate=gate, crop_dir=crop_dir)
            except Exception:
                log.exception("多帧识别单帧异常: %s", path)
                result = RecognitionResult(need_manual=True, source_status="unreadable",
                                           message="单帧识别失败")
                self._log(gate, path, result)
            results.append(result)
        if frame_results is not None:
            frame_results.extend(results)
        fused = self.fuser.fuse(results)
        valid = [r for r in results if r.plate]
        trusted = [r for r in valid if r.success and not r.need_manual and not r.suspected]
        same_target = (len({(r.plate, r.color) for r in valid}) == 1)
        grammar_ok = bool(fused.plate and fused.color and
                          validate_grammar(fused.plate, fused.color.value)[0])
        corroborated = (distinct_frames and len(trusted) >= 2 and same_target and grammar_ok and
                        fused.plate == trusted[0].plate and fused.color == trusted[0].color)
        fused.success = bool(fused.success and corroborated)
        fused.need_manual = not fused.success
        if not corroborated:
            fused.message = "多帧证据不足或车牌冲突，请人工确认"
        fused.elapsed_ms = sum(r.elapsed_ms for r in results)
        self._log(gate, image_paths[0] if image_paths else None, fused)
        return fused

    def is_available(self) -> bool:
        cls = getattr(self.recognizer, "classifier", None)
        return bool(getattr(cls, "available", True))
