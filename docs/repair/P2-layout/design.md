# P2-07 安全布局热更新
只读场地有80个确定车位；任意anchor/columns/rows/spacing会生成路网没有的车位，C20→25与只读几何冲突。保持纯生成器兼容，运行中LayoutService拒绝与manifest车位几何不一致的声明，只允许已有网格内启停/类型变更，不修改资产。数量可缩至1并恢复至20，超过20或移动几何返回409；占用车位仍禁止移除。

前端串行合并布局重拉，刷新/重连若版本不同也重拉；applyLayout重建标线/编号/索引/marker，刷新静态碰撞注册；planner.setSpaces原位更新，reservations/当前车辆路径保持，scheduler显式重新绑定planner/zones并清空zone缓存。由于不接受几何移动，现有车辆路径仍有效；固定壳/道具保持manifest位置，新增/恢复车位不改变模型。验收双端C20→18→20及类型变更、API/规划器/marker/碰撞体一致、原预约对象保留、 occupied删除拒绝、C25/自定义坐标拒绝；截图与history审计。

## 物理能力边界（2026-10-23 复核，P2-07）

只读场地（`assets_3d/layout/lot_v2.json`，manifest 为单一事实源）当前能力：
- A/B/C/D 每区 = 10 列 × 2 排 = 20，标准柱距 3.0 m；C/D 翼宽 30 m（`lot.x_max` 36 − `space_grid.C.x_start` 6），车道链节点止于 x=36。
- 运行时不移动几何：只允许 manifest 网格内的数量收缩/恢复（1..20/区）与类型变更；occupied 车位不可移除。

C25 / 自定义坐标的物理结论（实测 `_qa_audit/repair_tasks_20261023/P2-07/evidence/physics-capacity.json`）：
- 25 个/区需 `ceil(25/2)=13` 列；3.0 m 柱距需 39 m 翼宽 ⇒ 需向东扩容 9 m，并重建 `lot_shell.glb`（Blender 工具链，当前环境未安装）、延长车道链并重跑 route-v2 160 路线/碰撞/满停邻车/20 车/C-10 审计。
- 强行塞入 30 m（柱距 2.3077 m）：van 邻车扫掠相交 24 次、最小净距 0、贴墙 1.69 m；柱距 2.4 m 相交 10 次；2.6 m 净距 0.081 m 且贴墙 0.143 m。安全柱距下限 = 3.0 m（实测最小净距 0.456 m）。
- 自定义 anchor/columns/rows/spacing/heading 同样会破坏"边界避墙曲线按 manifest 网格标定"与既有动线证据，不可在运行时开放。

因此本设计的能力边界保持"只读 80 车位网格"，但**错误从"静默拒绝"升级为结构化、可预演**：
- `POST /api/admin/layout` 返回 409 + `detail.reason_code`（`capacity_exceeded` / `geometry_out_of_manifest`）与 `capacity/needed_columns/needed_wing_width_m/site_expansion_m`、`violations[]`、`layout_constraints`；
- 新增 `POST /api/admin/layout/validate`（admin）预演同一请求，**不落库/不写历史/不广播**；
- `GET /api/layout` 增加 `layout_constraints`（每区容量/柱距/翼宽/行位、`min_safe_pitch_m`、`c25` 可行性、规则），前端无需改动（附加字段）。

**状态：P2-07 = 部分修复 / 需求降级（C25 未支持）**。真正支持 C25 需完成场地扩容（+9 m）、`lot_shell.glb` 重建、manifest/生成器/车道链扩展与全量动线重新基线；详见 `_qa_audit/repair_tasks_20261023/P2-07/REPORT.md` §6。
