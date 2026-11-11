"""场景剧本存储/校验（B/C 后端）。

新格式（仿真秒）::

    {
      "name": "...", "description": "...", "seed": 20261024,
      "time_unit": "sim_sec",
      "vehicles": [
        {"vid": "E1", "t": 1.0, "plate": "湘N7D071",
         "plate_type": "blue", "duration": 600.0}
      ],
      "steps": [...]          # 可选；兼容旧 spawn/exit/pay/... 格式
    }

``t``/``duration`` 均以**仿真秒**表示；后端只负责校验/存取，不负责 3D 播放。
``GET /sim/scenario`` 优先 ``data/scenario.json``，回退内置默认。
"""
from __future__ import annotations

import json
import logging
import math
import random
from pathlib import Path
from typing import Any, Optional

from .core.config import AppConfig
from .lpr import charset

log = logging.getLogger("scenario")

SCENARIO_FILENAME = "scenario.json"
ALLOWED_ACTIONS = {
    "spawn", "exit", "pay", "safety_probe", "inject", "recover", "wait",
    "zone", "passed", "left", "barrier_ack",
}
MAX_VEHICLES = 50
MAX_STEPS = 500
MAX_DURATION_SEC = 24 * 3600.0


def scenario_path(cfg: AppConfig) -> Path:
    return Path(cfg.paths.data_dir) / SCENARIO_FILENAME


def _number(value: Any, field: str, *, minimum: float = 0.0,
            maximum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须为数字")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须为有限数字")
    if number < minimum:
        raise ValueError(f"{field} 不能小于 {minimum:g}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} 不能大于 {maximum:g}")
    return number


#: 默认 6 车固定车牌：qa-sim 外部 18 牌 3D 验证（入口 18/18、出口 18/18 exact）
#: 后推荐组合；覆盖 blue×2 / green×2 / green_large / yellow 与免费/收费/多车时序。
_FIXED_DEFAULT_PLATES: list[tuple[str, str, str, float, float]] = [
    # (vid, plate, plate_type, t, duration)
    ("E1", "粤F4K1CZ", "blue", 1.0, 600.0),          # 10 分钟：免费
    ("E2", "贵JD8H5PF", "green", 4.0, 1800.0),       # 30 分钟：收费（新能源折扣）
    ("E3", "鄂ABWTXHF", "green_large", 90.0, 1200.0),
    ("E4", "桂ES6EKB", "yellow", 120.0, 2400.0),
    ("E5", "甘N35MYY", "blue", 180.0, 960.0),        # 16 分钟：超过免费时长
    ("E6", "渝GDKLV32", "green", 240.0, 300.0),
]


def _default_vehicles(seed: int = 20261024) -> list[dict]:
    """默认 6 车：**固定车牌**（3D 外部真实验证组合），免费/收费/多车相继、不同类型。

    保持 ``seed`` 参数仅为兼容既有调用；车牌不再随机生成，确保 3D/后端/文档一致。
    """
    vehicles = []
    used: set[str] = set()
    for vid, plate, plate_type, t, duration in _FIXED_DEFAULT_PLATES:
        ok, reason = charset.validate_plate_type(plate, plate_type)
        if not ok:  # pragma: no cover - 常量在测试中已校验
            raise ValueError(f"默认剧本车牌非法 {plate}/{plate_type}: {reason}")
        if plate in used:  # pragma: no cover
            raise ValueError(f"默认剧本车牌重复: {plate}")
        used.add(plate)
        vehicles.append({
            "vid": vid, "t": t, "plate": plate,
            "plate_type": plate_type, "duration": duration,
        })
    return vehicles


def _default_steps(vehicles: list[dict]) -> list[dict]:
    """旧 steps 兼容视图；新前端可忽略，直接按 vehicles 播放。"""
    steps: list[dict] = []
    for vehicle in vehicles:
        steps.append({"t": vehicle["t"], "action": "spawn", "vid": vehicle["vid"],
                      "plate": vehicle["plate"], "plate_type": vehicle["plate_type"]})
    # 复杂流程：安全探测 + 道闸故障/恢复 + 多车相继出场
    steps.append({"t": 3.0, "action": "safety_probe", "gate": "in", "timeout": 60})
    for vehicle in vehicles:
        exit_t = round(vehicle["t"] + vehicle["duration"], 3)
        steps.append({"t": exit_t, "action": "pay", "vid": vehicle["vid"],
                      "timeout": 240})
        steps.append({"t": exit_t + 3.0, "action": "exit", "vid": vehicle["vid"],
                      "timeout": 240})
    steps.append({"t": 300.0, "action": "inject", "kind": "barrier", "gate": "out",
                  "mode": "ack_timeout"})
    steps.append({"t": 308.0, "action": "recover", "kind": "barrier", "gate": "out"})
    steps.append({"t": 310.0, "action": "wait"})
    steps.sort(key=lambda item: (float(item["t"]), item["action"]))
    return steps


def builtin_scenario() -> dict:
    vehicles = _default_vehicles()
    return {
        "name": "默认演示剧本：免费/收费/多车/复杂路径",
        "description": (
            "覆盖免费时段停车（10 分钟）、收费时段停车（30/40/16 分钟）、"
            "6 车相继进场出场、不同类型（蓝/小绿/大绿/黄）与复杂道闸故障恢复；"
            "t/duration 单位为**仿真秒**（sim_sec），后端只存取不播放。"
        ),
        "seed": 20261024,
        "time_unit": "sim_sec",
        "vehicles": vehicles,
        "steps": _default_steps(vehicles),
    }


def _validate_steps(steps: Any, vehicle_ids: set[str]) -> list[dict]:
    if steps is None:
        return []
    if not isinstance(steps, list):
        raise ValueError("steps 必须为数组")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"steps 不能超过 {MAX_STEPS} 条")
    normalized = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"steps[{index}] 必须为对象")
        item = dict(step)
        item["t"] = _number(item.get("t"), f"steps[{index}].t")
        action = str(item.get("action", "")).strip()
        if action not in ALLOWED_ACTIONS:
            raise ValueError(
                f"steps[{index}].action 非法: {action!r}；允许 "
                + "/".join(sorted(ALLOWED_ACTIONS)))
        item["action"] = action
        vid = item.get("vid")
        if vid is not None and vehicle_ids and str(vid) not in vehicle_ids:
            raise ValueError(f"steps[{index}].vid {vid!r} 未在 vehicles 中定义")
        normalized.append(item)
    return normalized


def validate_scenario(raw: Any) -> dict:
    """校验并规范化场景（新 vehicles 格式 + 旧 steps 兼容）。"""
    if not isinstance(raw, dict):
        raise ValueError("场景必须为 JSON 对象")
    data = dict(raw)
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("name 不能为空")
    data["name"] = name
    if "description" not in data:
        data["description"] = ""
    data["description"] = str(data["description"])
    if "seed" in data and data["seed"] is not None:
        if isinstance(data["seed"], bool) or not isinstance(data["seed"], int):
            raise ValueError("seed 必须为整数")
    else:
        data["seed"] = 20261024
    vehicles_raw = data.get("vehicles")
    vehicles: list[dict] = []
    if vehicles_raw is not None:
        if not isinstance(vehicles_raw, list):
            raise ValueError("vehicles 必须为数组")
        if len(vehicles_raw) > MAX_VEHICLES:
            raise ValueError(f"vehicles 不能超过 {MAX_VEHICLES} 辆")
        seen: set[str] = set()
        for index, vehicle in enumerate(vehicles_raw):
            if not isinstance(vehicle, dict):
                raise ValueError(f"vehicles[{index}] 必须为对象")
            item = dict(vehicle)
            vid = str(item.get("vid", "")).strip()
            if not vid:
                raise ValueError(f"vehicles[{index}].vid 不能为空")
            if vid in seen:
                raise ValueError(f"vehicles 存在重复 vid: {vid}")
            seen.add(vid)
            item["vid"] = vid
            item["t"] = _number(item.get("t"), f"vehicles[{index}].t")
            item["duration"] = _number(
                item.get("duration"), f"vehicles[{index}].duration",
                minimum=0.001, maximum=MAX_DURATION_SEC)
            plate_type = str(item.get("plate_type", "")).strip().lower()
            plate, plate_type = charset.normalize_plate_type_plate(
                str(item.get("plate", "")), plate_type)
            item["plate"] = plate
            item["plate_type"] = plate_type
            vehicles.append(item)
        data["vehicles"] = vehicles
    # 实现 B：同一车牌在重叠时段只允许一次入场（业务上不可能同时在场两辆同牌车）。
    by_plate: dict[str, list[dict]] = {}
    for _vehicle in vehicles:
        by_plate.setdefault(_vehicle["plate"], []).append(_vehicle)
    for _plate, _group in by_plate.items():
        if len(_group) < 2:
            continue
        _group.sort(key=lambda v: (float(v["t"]), float(v["duration"])))
        for _prev, _cur in zip(_group, _group[1:]):
            _prev_end = float(_prev["t"]) + float(_prev["duration"])
            if float(_cur["t"]) < _prev_end - 1e-9:
                raise ValueError(
                    f"同一车牌 {_plate} 的入场时段重叠：{_prev['vid']} "
                    f"[{_prev['t']}, {_prev_end:g}) 与 {_cur['vid']} "
                    f"[{_cur['t']}, {float(_cur['t']) + float(_cur['duration']):g})")
    steps_raw = data.get("steps")
    if steps_raw is None and "actions" in data:
        steps_raw = data["actions"]
    if steps_raw is not None:
        data["steps"] = _validate_steps(steps_raw, {v["vid"] for v in vehicles})
        if "actions" in data and "steps" not in raw:
            data.pop("actions", None)
    if not vehicles and not data.get("steps"):
        raise ValueError("vehicles 不能为空（旧 steps 剧本至少也要有 steps）")
    data.setdefault("time_unit", "sim_sec")
    return data


def load_scenario(cfg: AppConfig) -> tuple[dict, str]:
    """返回 (scenario, source)；data 文件优先，缺失/损坏回退内置。"""
    path = scenario_path(cfg)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            scenario = validate_scenario(raw)
            scenario["source"] = "data"
            return scenario, "data"
        except Exception:  # noqa: BLE001 - 损坏文件不阻断仿真端
            log.exception("data/scenario.json 解析/校验失败，回退内置默认")
    scenario = builtin_scenario()
    scenario["source"] = "builtin"
    return scenario, "builtin"


def save_scenario(cfg: AppConfig, raw: Any) -> dict:
    scenario = validate_scenario(raw)
    path = scenario_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    temp.replace(path)
    scenario["source"] = "data"
    return scenario


def reset_scenario(cfg: AppConfig) -> dict:
    path = scenario_path(cfg)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            log.exception("删除 data/scenario.json 失败")
    scenario = builtin_scenario()
    scenario["source"] = "builtin"
    return scenario
