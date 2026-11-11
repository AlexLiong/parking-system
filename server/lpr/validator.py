"""规则校验与易混字符仲裁（lpr/validator.py）——流水线 ⑦（鲁棒性核心 RR-1.2）。

三层策略：
1. **位置语法硬约束**：按 char_set_for_position 过滤非法候选（序号位排除汉字，
   第 2 位排除 I/O 等），多数"0/O"歧义可直接排除其一；
2. **专项仲裁**：当某位 top1/top2 置信度差 < 阈值且落入易混对时，结合业务上下文
   （出场时与在场库比对）选最可能字符，仍不确定则标 ``suspected``；
3. **综合置信度**：``overall = Π(char_conf)^(1/n)``，低于阈值转人工确认（FR-9.3）。
"""
from __future__ import annotations

import math

from ..core.domain import PlateContext, ValidationResult, PlateColor
from . import charset


def _head_for_pos(pos: int) -> str:
    if pos == 0:
        return "hanzi"
    if pos == 1:
        return "letter"
    return "alnum"


class PlateValidator:
    def __init__(self, disambig_gap: float = 0.15, conf_threshold: float = 0.85):
        self.disambig_gap = disambig_gap
        self.conf_threshold = conf_threshold

    def _pick(self, options: list[tuple[str, float]], allowed: set[str]) -> list[tuple[str, float]]:
        """过滤到合法字符集并保持置信度降序。"""
        filtered = [(c, s) for (c, s) in options if c in allowed]
        filtered.sort(key=lambda t: t[1], reverse=True)
        return filtered

    def validate(self, chars: list[list[tuple[str, float]]], color: str | PlateColor,
                 context: PlateContext | None = None) -> ValidationResult:
        context = context or PlateContext()
        color_val = color.value if isinstance(color, PlateColor) else (color or "blue")
        n = charset.expected_len(color_val)

        # 位数不符（分割异常）：直接判失败，但保留候选供人工
        if len(chars) != n:
            best = "".join(
                (opts[0][0] if opts else "?") for opts in chars
            )
            return ValidationResult(plate=best or None, color=None, overall_conf=0.0,
                                    valid=False, suspected=True,
                                    reason=f"分割字符数 {len(chars)} != 期望 {n}")

        chosen: list[str] = []
        confs: list[float] = []
        ambiguous: list[tuple[int, list[tuple[str, float]]]] = []

        for i in range(n):
            allowed = charset.char_set_for_position(i, color_val)
            head = _head_for_pos(i)
            opts = self._pick(chars[i], allowed)
            if not opts:
                # 该位无合法候选：用原始 top1 兜底并标记
                opts = chars[i][:1] or [("?", 0.0)]
            top_char, top_conf = opts[0]
            chosen.append(top_char)
            confs.append(max(0.0, min(1.0, top_conf)))
            # 易混判定
            if len(opts) >= 2:
                second_char, second_conf = opts[1]
                if (top_conf - second_conf) < self.disambig_gap and \
                        charset.is_confusable(top_char, second_char):
                    ambiguous.append((i, opts[:2]))

        # 上下文仲裁（出场：优先匹配在场车牌）
        suspected = False
        if ambiguous and context.gate == "out" and context.in_lot_plates:
            resolved = self._match_in_lot(chosen, ambiguous, context.in_lot_plates)
            if resolved is not None:
                chosen = list(resolved)
                suspected = True

        plate = "".join(chosen)
        overall = self._geometric_mean(confs) if confs else 0.0
        ok, reason = charset.validate_grammar(plate, color_val)

        # 生成候选（含易混变体）供人工确认
        candidates = self._build_candidates(chosen, ambiguous, k=3)
        if ambiguous:
            suspected = True

        need_manual = (not ok) or (overall < self.conf_threshold)
        return ValidationResult(
            plate=plate if ok else plate,
            color=PlateColor(color_val),
            char_confs=[round(c, 4) for c in confs],
            overall_conf=round(overall, 4),
            candidates=candidates,
            valid=ok,
            suspected=suspected or (not ok),
            reason=reason or ("低置信度" if overall < self.conf_threshold else ""),
        )

    @staticmethod
    def _geometric_mean(confs: list[float]) -> float:
        if not confs:
            return 0.0
        # 避免 0 拉平：加极小量
        prod = 1.0
        for c in confs:
            prod *= max(c, 1e-6)
        return prod ** (1.0 / len(confs))

    def _match_in_lot(self, chosen: list[str],
                      ambiguous: list[tuple[int, list[tuple[str, float]]]],
                      in_lot: list[str]) -> list[str] | None:
        """在易混位的候选组合中，寻找与在场车牌完全匹配的组合。"""
        in_lot_set = set(in_lot)
        base = list(chosen)
        # 逐个易混位尝试第二候选（组合规模很小，通常 1~2 个易混位）
        from itertools import product
        options_per_pos = []
        positions = []
        for pos, opts in ambiguous:
            positions.append(pos)
            options_per_pos.append([c for c, _ in opts[:2]])
        for combo in product(*options_per_pos):
            cand = list(base)
            for pos, ch in zip(positions, combo):
                cand[pos] = ch
            if "".join(cand) in in_lot_set:
                return cand
        return None

    def _build_candidates(self, chosen: list[str],
                          ambiguous: list[tuple[int, list[tuple[str, float]]]],
                          k: int = 3) -> list[str]:
        base = "".join(chosen)
        cands = [base]
        for pos, opts in ambiguous:
            for ch, _ in opts[1:2]:
                variant = list(chosen)
                variant[pos] = ch
                s = "".join(variant)
                if s not in cands:
                    cands.append(s)
        return cands[:k]
