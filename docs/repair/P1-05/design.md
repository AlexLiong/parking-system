# P1-05 / P2-09 统一暂停与时钟
基线82bc044。原暂停按钮仅停止剧本；调试paused仅停车辆，scene/rig/后端tick继续。
选择后端权威DemoClock，显式demo模式支持暂停/速度控制；生产不提供暂停。状态（paused,speed,sim_time,revision）随snapshot和simulation_state事件同步所有客户端。后端tick与控制共用写锁：暂停不推进道闸超时；控制响应后无竞态tick。重启安全恢复为未暂停，瞬态道闸按既有恢复语义落安全侧，暂停不作为生产功能持久恢复。
前端暂停按钮走API，再应用权威状态；无后端preview本地暂停。动画循环对同一sim dt推进车辆/剧本/道闸/相机，低FPS用<=50ms子步消费实际经过时间，恢复不累计暂停间隔。OrbitControls禁用并跳过update避免阻尼漂移；HUD另显示仿真时间（墙钟继续真实显示供业务时间核对）。调试setPaused异步返回完成，step仅preview允许，禁止与后端时钟分叉。
验证：运动中暂停5s角度/车辆/相机/剧本/仿真时间不动，backend tick不FAULT；恢复继续；两个客户端同步；非demo403。低FPS1.5s动作不会因为前端dt截断而落后。
回退独立提交，无DB迁移。新增API POST /api/simulation/control {paused?,speed?}，GET /api/simulation；WS additive simulation_state。
