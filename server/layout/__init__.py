"""车位布局模块（layout）。

声明式布局（§3.9）：管理员只声明"区 + 数量 + 类型"，``LayoutGenerator`` 负责
翻译成与 3D 场景对齐的精确坐标（route-v2 后坐标以 ``assets_3d/layout/lot_v2.json``
manifest 为准），``LayoutService`` 负责落库（DB 为唯一事实源）并广播 LayoutChanged。

- ``LayoutGenerator``：纯函数式几何生成 + 静态校验 + 差异计算；
- ``LayoutService``：读写车位、版本留痕、运行时安全校验、事件广播；
- ``manifest``：route-v2 单一事实源的加载/校验/场景值映射（缺失回退旧常量）；
- ``migrate``：旧库布局坐标迁移（tools/migrate_layout_v2.py 的库实现）。
"""
from .generator import (DEFAULT_ZONE_GEOM, LayoutGenerator, ZoneGeom,
                        decls_from_dicts, zone_geom_from_manifest)
from .manifest import (Manifest, ManifestError, get_manifest, load_manifest,
                       manifest_gates_values, manifest_scene_values,
                       resolve_assets_root, resolve_manifest_path,
                       scene_config_issues, validate_manifest)
from .service import LayoutService

__all__ = [
    "LayoutGenerator", "LayoutService", "ZoneGeom",
    "DEFAULT_ZONE_GEOM", "decls_from_dicts", "zone_geom_from_manifest",
    "Manifest", "ManifestError", "load_manifest", "get_manifest",
    "validate_manifest", "manifest_scene_values", "manifest_gates_values",
    "resolve_manifest_path", "resolve_assets_root", "scene_config_issues",
]
