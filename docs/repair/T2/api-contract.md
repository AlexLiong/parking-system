# T2 识别结果与摄像头健康事件契约

`PlateRecognized` 在原有字段后增添可选 `reason`、`message`；WS `plate_recognized` 同名透传，旧客户端忽略新增字段即可兼容。

| reason | 含义 | 摄像头源健康计数 |
| --- | --- | --- |
| `ok` | 识别或人工授权完成 | 有效图像事件重置源失败计数；人工动作不计 |
| `low_conf` | 图像可读但未定位、低置信或多车牌待确认 | 重置源失败计数，不登记 camera 异常 |
| `recognize_error` / `model_unavailable` | 推理异常或模型不可用 | 不改变源健康计数，转人工 |
| `camera_missing` | 到达事件缺图像路径 | 连续源失败计数 +1 |
| `image_unreadable` | 路径不可读或图像损坏 | 连续源失败计数 +1 |
| `camera_blank` | 可解码但近乎均匀的黑屏/白屏 | 连续源失败计数 +1 |
| `camera_fault` | 已存在未闭环 camera 异常 | 不重复计数 |
| 业务错误码 | `duplicate_entry`、`lot_full`、`no_parking_record` 等 | 不改变源健康计数 |

连续 `camera_missing`/`image_unreadable`/`camera_blank` 达到 `camera_fail_streak` 才登记 camera incident。识别模型失败、低置信和业务拒绝不能驱动摄像头故障。管理员注入与恢复 API 保持不变；恢复关闭 camera incident 并清零计数。

`GET /api/camera/{gate}/status`（需管理员令牌）返回 `gate/down/source_fail_streak/fail_threshold/incident_ids`；`POST /api/camera/{gate}/reset` 是现有 `/recover` 的别名，请求体仍为 `{reason, resolution}`。黑屏/白屏按 `camera_blank` 计数；仅亮度均值接近两端且灰度标准差小于 5 时判定，失焦指标只记录供人工审查，不触发设备故障。

此契约不把客户端任意无图事件当作设备硬件故障的确证，只作为图像源连续不可用的降级信号。真实相机的断连/超时健康探针仍待接入。
