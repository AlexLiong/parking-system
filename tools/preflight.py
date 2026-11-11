"""Fail before startup when the read-only scene or local inference resources are absent."""
from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

EXTERNAL_GENERATOR_DIR = ROOT / 'datasets/external/chinese_license_plate_generator'


def check_external_generator(root: Path = ROOT) -> dict:
    """M5：检查外部中文车牌生成器关键文件并可导入；缺失时给出明确可操作错误。"""
    base = Path(root) / 'datasets/external/chinese_license_plate_generator'
    required = ['plate_number.py', 'LICENSE']
    missing = [name for name in required if not (base / name).is_file()]
    if missing:
        raise RuntimeError(
            f"缺少外部中文车牌生成器关键文件 {missing}（目录 {base}）；"
            "请恢复 datasets/external/chinese_license_plate_generator 后重试"
            "（参见 tools/generate_reference_plates.py 的 SOURCE 路径说明）")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_clp_plate_number_ext', base / 'plate_number.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('无法加载 plate_number.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 可导入检查失败需明确报错
        raise RuntimeError(
            f"外部中文车牌生成器不可导入（{base / 'plate_number.py'}）：{exc}") from exc
    return {'external_generator': str(base),
            'external_plate_number_module': getattr(module, '__name__', 'plate_number')}


def check():
    from server.core.config import load_config
    from server.layout.manifest import load_manifest
    from server.lpr.hyperlpr_adapter import verify_models
    from server.lpr.province_verifier import templates
    from server.lpr.render import PlateFonts
    cfg=load_config()
    manifest=load_manifest(cfg=cfg,strict=True)
    if manifest is None:raise RuntimeError('缺少有效 assets_3d/layout/lot_v2.json；拒绝回退旧场景')
    verify_models();templates();PlateFonts()
    required=['web/admin/index.html','web/sim3d/index.html','web/sim3d/vendor/three.module.js',
              'web/sim3d/assets/lot_shell.glb','datasets/demo-lpr/manifest.json','hyperlpr3/LICENSE',
              'server/lpr/models/templates/meta.json']
    for name in required:
        if not (ROOT/name).is_file():raise FileNotFoundError(name)
    external = check_external_generator()
    return {'status':'ok','root':str(ROOT),'manifest':str(manifest.path),
            'models':'SHA256 verified','demo_gallery':18,
            **external}
if __name__=='__main__':
    try:print(json.dumps(check(),ensure_ascii=False))
    except Exception as exc:print(f'启动检查失败: {exc}',file=sys.stderr);raise SystemExit(1)
