"""车位布局生成器与服务测试（layout/generator.py, layout/service.py）。"""
from __future__ import annotations

import pytest

from server.core.domain import SpaceKind, ZoneDecl
from server.core.errors import LayoutConflict
from server.layout.generator import LayoutGenerator, decls_from_dicts


# ----------------------------- 生成器（纯函数） -----------------------------
def test_generate_standard_zone():
    gen = LayoutGenerator()
    specs = gen.generate([ZoneDecl(zone="A", count=20)])
    assert len(specs) == 20
    assert specs[0].space_no == "A-01"
    assert specs[-1].space_no == "A-20"
    # 编号唯一、坐标唯一
    assert len({s.space_no for s in specs}) == 20
    assert len({(s.x, s.z) for s in specs}) == 20


def test_bootstrap_c_zone_is_ne_prefer(container):
    # config.default_layout 显式声明 C 区为新能源优先
    c_spaces = container.space_repo.free_in_zone("C")
    assert c_spaces and all(sp.kind == "ne_prefer" for sp in c_spaces)
    # A 区为普通车位
    a_spaces = container.space_repo.free_in_zone("A")
    assert all(sp.kind == "normal" for sp in a_spaces)


def test_generate_custom_kind_overrides():
    gen = LayoutGenerator()
    specs = gen.generate([ZoneDecl(zone="A", count=5, kind=SpaceKind.NE_PREFER)])
    assert all(s.kind == SpaceKind.NE_PREFER for s in specs)


def test_validate_rejects_empty():
    gen = LayoutGenerator()
    with pytest.raises(LayoutConflict):
        gen.generate([])


def test_validate_rejects_duplicate_zone():
    gen = LayoutGenerator()
    with pytest.raises(LayoutConflict):
        gen.generate([ZoneDecl(zone="A", count=5), ZoneDecl(zone="a", count=5)])


def test_validate_rejects_nonpositive_count():
    gen = LayoutGenerator()
    with pytest.raises(LayoutConflict):
        gen.generate([ZoneDecl(zone="A", count=0)])


def test_validate_rejects_over_capacity():
    gen = LayoutGenerator()
    # 默认 columns=10 rows=2 → 容量 20
    with pytest.raises(LayoutConflict):
        gen.generate([ZoneDecl(zone="A", count=21)])


def test_diff_added_removed_changed():
    gen = LayoutGenerator()
    old_specs = gen.generate([ZoneDecl(zone="A", count=10)])
    old_map = {s.space_no: s for s in old_specs}
    new_specs = gen.generate([ZoneDecl(zone="A", count=8)])   # 缩减 → 移除 2
    diff = gen.diff(old_map, new_specs, version=3)
    assert diff.version == 3
    assert set(diff.removed) == {"A-09", "A-10"}
    assert diff.added == []


def test_decls_from_dicts_normalizes():
    decls = decls_from_dicts([{"zone": "a", "count": 5, "kind": "NE_PREFER"}])
    assert decls[0].zone == "A"
    assert decls[0].kind == SpaceKind.NE_PREFER


# ----------------------------- 服务（需 DB） -----------------------------
def test_bootstrap_creates_default_layout(container):
    spaces = container.space_repo.list_active()
    # config.default_layout: A/B/C/D 各 20 = 80
    assert len(spaces) == 80
    zones = container.space_repo.total_count_by_zone()
    assert zones == {"A": 20, "B": 20, "C": 20, "D": 20}


def test_current_layout_shape(container):
    layout = container.layout.current_layout()
    assert layout.free_total == 80
    assert set(layout.zones.keys()) == {"A", "B", "C", "D"}
    assert "entry" in layout.gates and "exit" in layout.gates


def test_apply_layout_version_increments(container):
    v0 = container.layout.current_version()
    with container.db.scope():
        diff = container.layout.apply_layout(
            [{"zone": "A", "count": 20}, {"zone": "B", "count": 10}],
            operator="tester")
    assert diff.version == v0 + 1
    # B 区缩减到 10，A 区保留 20
    assert container.space_repo.total_count_by_zone()["B"] == 10


def test_cannot_remove_occupied_space(container):
    # 占用一个将被移除的高编号 B 区车位后，缩减 B 区应被安全校验拦截
    with container.db.scope():
        sp = container.space_repo.free_in_zone("B")[-1]   # B-20：缩减到 5 后会被移除
        container.space_repo.occupy(sp.space_no, "琼A00001")
        occupied_no = sp.space_no
    with pytest.raises(LayoutConflict):
        with container.db.scope():
            container.layout.apply_layout(
                [{"zone": "A", "count": 20}, {"zone": "B", "count": 5}],
                operator="tester")
    # 拦截后车位仍处于占用（事务回滚，未被误删）
    assert container.space_repo.get(occupied_no).status == "occupied"


def test_runtime_rejects_custom_geometry_without_changing_history(container):
    version = container.layout.current_version()
    with pytest.raises(LayoutConflict):
        with container.db.scope():
            container.layout.apply_layout([{'zone': 'A', 'count': 25, 'rows': 3,
                                            'anchor': [-36, 25], 'columns': 10}])
    assert container.layout.current_version() == version
    assert container.space_repo.active_total() == 80
    with pytest.raises(LayoutConflict):
        with container.db.scope():
            container.layout.apply_layout([{'zone': 'A', 'count': 20, 'anchor': [-30, 25]}])


def test_runtime_reduce_restore_keeps_manifest_coordinates(container):
    original = container.layout.specs_from_db()
    for n in (18, 20):
        with container.db.scope():
            container.layout.apply_layout([{'zone': z, 'count': n if z == 'C' else 20,
                                            'kind': 'ne_prefer' if z == 'C' else 'normal'}
                                           for z in 'ABCD'])
        current = container.layout.specs_from_db()
        assert len(current) == 60 + n
        assert all((sp.x, sp.z, sp.rotation) == (original[no].x, original[no].z, original[no].rotation)
                   for no, sp in current.items())


# ---------------------------------------------------------------------------
# P2-07 布局能力边界（2026-10-23）：
#   只读场地物理容量 20/区（10 列 × 2 排，柱距 3 m，翼宽 30 m）；C25 需 13 列 39 m 翼宽
#   （向东扩容 9 m + 重建 lot_shell.glb/路网）。运行时对 C25/自定义坐标返回结构化 409，
#   并提供 /api/admin/layout/validate 预演（不落库）。以下测试锁定该能力边界与错误可读性。
# ---------------------------------------------------------------------------
def test_c25_rejected_with_capacity_reason(container):
    """C25 仍 409：保留 manifest 固定网格校验，但不再有预演/能力字段。"""
    version = container.layout.current_version()
    with pytest.raises(LayoutConflict) as ei:
        with container.db.scope():
            container.layout.apply_layout([{'zone': 'C', 'count': 25}])
    detail = ei.value.detail
    assert detail['reason_code'] == 'capacity_exceeded'
    assert detail['zone'] == 'C'
    assert detail['requested'] == 25
    assert detail['capacity'] == 20
    assert detail['violations'][0]['reason_code'] == 'capacity_exceeded'
    assert '容量' not in detail.get('message', 'x') or '最多' in ei.value.message
    assert 'needed_wing_width_m' not in detail and 'site_expansion_m' not in detail
    assert container.layout.current_version() == version
    assert container.space_repo.active_total() == 80


def test_custom_coordinates_rejected_with_reason(container):
    version = container.layout.current_version()
    with pytest.raises(LayoutConflict) as ei:
        with container.db.scope():
            container.layout.apply_layout([{
                'zone': 'C', 'count': 8, 'anchor': [8.0, 25.75],
                'columns': 4, 'rows': 2, 'spacing': 3.0,
            }])
    detail = ei.value.detail
    assert detail['reason_code'] == 'geometry_out_of_manifest'
    assert detail['zone'] == 'C' and detail['space_no'] == 'C-01'
    assert 'manifest' in ei.value.message and '运行时' in ei.value.message
    assert container.layout.current_version() == version
    assert container.space_repo.active_total() == 80


def test_custom_rows_rejected_with_geometry_reason(container):
    with pytest.raises(LayoutConflict) as ei:
        with container.db.scope():
            container.layout.apply_layout([{
                'zone': 'A', 'count': 20, 'rows': 3, 'anchor': [-36, 25], 'columns': 10,
            }])
    assert ei.value.detail['reason_code'] == 'geometry_out_of_manifest'
    assert container.space_repo.active_total() == 80


def test_layout_has_no_preview_or_capability_fields(client, admin_token):
    data = client.get('/api/layout').json()
    for key in ('layout_constraints', 'zone_capacity', 'runtime_geometry_fixed'):
        assert key not in data
    # 预演端点已删除
    r = client.post('/api/admin/layout/validate',
                    json={'zones': [{'zone': 'C', 'count': 25}]},
                    headers=admin_token)
    assert r.status_code == 404
    # C25 apply 仍返回 409（固定网格校验保留）
    a = client.post('/api/admin/layout', json={'zones': [{'zone': 'C', 'count': 25}]},
                    headers=admin_token)
    assert a.status_code == 409
    assert a.json()['detail']['reason_code'] == 'capacity_exceeded'
    # 收缩/恢复仍可用
    for n in (18, 20):
        r = client.post('/api/admin/layout',
                        json={'zones': [{'zone': z, 'count': n if z == 'C' else 20}
                                        for z in 'ABCD']},
                        headers=admin_token)
        assert r.status_code == 200

def test_equivalent_custom_geometry_keeps_manifest_grid(container):
    """显式 anchor/columns/rows/spacing 若等价于 manifest 网格应被接受（按实际几何判定，非按字段拒绝）。"""
    decls = [d for d in ({'zone': z, 'count': 20} for z in 'ABCD') if d['zone'] != 'C'] + [{
        'zone': 'C', 'count': 20, 'anchor': [6.0, 25.75],
        'columns': 10, 'rows': 2, 'spacing': 3.0,
    }]
    with container.db.scope():
        container.layout.apply_layout(decls, operator='p27-test', publish=False)
    current = container.layout.specs_from_db()
    canon = {sp.space_no: sp for sp in LayoutGenerator(manifest=container.layout.manifest).generate(
        [ZoneDecl(zone=z, count=20) for z in 'ABCD'])}
    assert len(current) == 80
    for no, sp in current.items():
        assert (sp.x, sp.z, sp.rotation) == (canon[no].x, canon[no].z, canon[no].rotation)

