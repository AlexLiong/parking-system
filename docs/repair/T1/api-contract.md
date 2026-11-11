# T1 API 与 WS 契约（设计稿）

所有时间使用带时区 ISO 8601 UTC；字段名沿用现有 snake_case。新字段只追加，不移除旧 REST/WS 字段。`seq` 为整数全局单调，`event_id`/`request_id`/`cmd_id` 为字符串 UUID。错误响应沿用 `{error,message,detail}`。

## REST

| 方法/路径 | 请求 | 成功响应 | 权限 |
| --- | --- | --- | --- |
| `GET /api/state/snapshot` | 可选 `session`, `since>=0` | `{seq,server_time,session,layout,on_lot,vehicle_runtime,barriers,cameras,pending_manual,open_incidents,config}`；`since` 不改变完整快照内容 | 已授权管理端/仿真端 |
| `GET /api/events` | `since>=0`, `limit=1..500`, 可选 `session`, `types` | `{items,next_since,has_more,head_seq}`；按 seq 升序；同步补发不加过滤参数 | 已授权客户端 |
| `POST /api/demo/session` | `{request_id,seed?,operator?}` | `{session,snapshot}`；active 已存在时返回现有会话 | 管理员 |
| `GET /api/demo/session/current` | 无 | `{session,snapshot}` | 已授权客户端 |
| `POST /api/demo/reset` | `{request_id,mode:"soft"|"hard",new_session:true,reason?}` | `{closed_session,new_session,reset_report,seq,snapshot}` | 管理员；审计 |
| `GET /api/barrier/actions` | `gate?`, `since?`, `limit?` | 动作日志分页 | 管理员 |
| `POST /sim/state` | `{session_id,owner_id,vehicles:[{vehicle_id,state,pose,legs,dist,...}]}` | `{ok,count}`；旧会话返回 409 | 已登录仿真端 |

`snapshot.layout` 至少有 `version/free_total/free_by_zone/spaces`；`on_lot[]` 至少有 `record_id/vehicle_id/plate/space_no/session_id/entry_time`；`barriers[gate]` 至少有 `state/arm_position/pending/last_cmd_id/fault`。待办和异常可以是空数组，字段必须存在。若 `session` 与当前会话不匹配，返回 `409 session_changed` 并附当前 session id；不能悄悄混入旧会话事件。

`POST /api/demo/reset` 的 `request_id` 必填；相同 id 与相同请求体返回第一次结果，id 相同但请求体不同返回 `409 idempotency_conflict`。闸区占用/未决业务交易返回 `409 reset_unsafe`，不部分更新。历史 `session_id=NULL` 记录默认不被清除。软/硬模式均保留审计。未授权返回 401/403。

## WS v2

连接：`/ws?features=v2&client=sim|admin&since=N&session=<uuid>`；浏览器以 WebSocket 子协议 `['v2','token.<JWT>']` 传凭据，服务端校验后选择 `v2` 子协议。受限仿真令牌在 T4 安全专项实现。服务端按以下顺序发送：

1. `{"type":"welcome","protocol":2,"session":"...","head_seq":1024,"heartbeat_sec":5}`。
2. 无有效 `since`：`{"type":"snapshot","data":{...}}`；有效游标：一条或多条 `{"type":"batch","events":[...]}`。
3. 实时：`{"type":"event","seq":1025,"event_id":"...","session_id":"...","name":"space_update","data":{...},"ts":"..."}`。

`type` 表示封套类型，业务类型使用 `name`，避免 `{"type":"event","type":"space_update"}` 键冲突。`batch.events[]` 与实时事件使用同一封套。服务端可发送 `{"type":"resync_required","reason":"gap|expired|session_changed","head_seq":...}`。客户端可发 `{"type":"ack","seq":N}` 供连接监控，但 ack 不删除 DB 事件；心跳 `ping/pong` 不进入业务事件流。

同步客户端只请求未过滤的全局流，应用 `seq=last_seq+1`，重复 `seq<=last_seq` 忽略，`seq>last_seq+1` 先用 REST 补发，补发失败再取快照。按 `session/types` 筛选的历史列表不能用于同步游标。会话变更事件 `DEMO_RESET` 必须先清旧会话本地状态，再 hydrate 新快照。`barrier_cmd` 的业务 data 保留旧 `gate/cmd/reason`，追加 `cmd_id`；`barrier_ack` 必须回同一 `cmd_id`。

旧客户端未传 `features=v2` 时仍收原 `welcome/sync/space_update/...` 消息。服务器在兼容期按连接协议分别编码，v2 客户端不能同时收到旧格式业务消息。`features.ws_v2=false` 是旧协议回滚开关，旧协议没有 T1 的无损补发保证。
