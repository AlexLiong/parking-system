"""多帧投票融合（lpr/fuser.py）——流水线 ⑧（FR-10.5 / RR-1.3）。

视频/连拍模式下，对同一车辆的多帧识别结果按位做置信度加权投票，
显著提升单帧误差下的整体正确率；整体仍不过阈值则由上层转人工。
"""
from __future__ import annotations

from collections import defaultdict

from ..core.domain import RecognitionResult, PlateColor


class MultiFrameFuser:
    def __init__(self, conf_threshold: float = 0.85):
        self.conf_threshold = conf_threshold

    def fuse(self, results: list[RecognitionResult]) -> RecognitionResult:
        results = [r for r in results if r.plate]
        if not results:
            return RecognitionResult(success=False, need_manual=True,
                                     message="多帧融合：无有效识别结果")
        if len(results) == 1:
            return results[0]

        # 以出现最多的长度为主（蓝 7 / 绿 8）
        len_votes: dict[int, float] = defaultdict(float)
        for r in results:
            len_votes[len(r.plate)] += max(r.overall_conf, 0.1)
        target_len = max(len_votes.items(), key=lambda kv: kv[1])[0]
        same = [r for r in results if len(r.plate) == target_len]

        # 颜色投票
        color_votes: dict[str, float] = defaultdict(float)
        for r in same:
            if r.color:
                color_votes[r.color.value] += max(r.overall_conf, 0.1)
        color = PlateColor(max(color_votes.items(), key=lambda kv: kv[1])[0]) \
            if color_votes else None

        # 逐位加权投票
        fused_chars = []
        fused_confs = []
        for pos in range(target_len):
            votes: dict[str, float] = defaultdict(float)
            for r in same:
                ch = r.plate[pos]
                w = r.char_confs[pos] if pos < len(r.char_confs) else r.overall_conf
                votes[ch] += max(w, 0.05)
            best_ch = max(votes.items(), key=lambda kv: kv[1])[0]
            total = sum(votes.values()) or 1.0
            fused_chars.append(best_ch)
            fused_confs.append(round(votes[best_ch] / total, 4))

        plate = "".join(fused_chars)
        overall = 1.0
        for c in fused_confs:
            overall *= max(c, 1e-6)
        overall = overall ** (1.0 / len(fused_confs)) if fused_confs else 0.0

        # 候选：各帧出现过的不同整牌
        cands = []
        for r in same:
            if r.plate not in cands:
                cands.append(r.plate)
        if plate not in cands:
            cands.insert(0, plate)

        elapsed = max(r.elapsed_ms for r in results)
        crop = next((r.plate_crop_path for r in results if r.plate_crop_path), None)
        return RecognitionResult(
            plate=plate, color=color, char_confs=fused_confs,
            overall_conf=round(overall, 4), candidates=cands[:3],
            plate_crop_path=crop, elapsed_ms=int(elapsed),
            success=overall >= self.conf_threshold,
            need_manual=overall < self.conf_threshold,
            message=f"多帧融合({len(same)}帧)",
        )
