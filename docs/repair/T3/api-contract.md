# T3 API

写接口均需管理员/操作员Bearer令牌；gate限in/out。POST /api/manual/{open,close,stop}-barrier {gate,reason}，POST /api/barrier/{gate}/keep-open {enabled}，POST /api/barrier/{gate}/clear-fault {}，POST /api/barrier/{gate}/fault {action:inject|clear,mode:ack_timeout|stuck_open|stuck_closed}。响应state/changed/cmd_id/seq/keep_open；物理危险返回409，可追溯op_log。GET /api/barrier/actions、/api/barrier/actions/export 返回带ID/操作者/状态/时间的动作记录，需令牌。

barrier_cmd保持cmd/cmd_id，增加target、duration_sec。barrier_state增加transient/reason/keep_open。barrier_ack含gate,state,cmd_id,arm_position（0..1，可选；停止回执必须带）。旧ack接受只限ws_v2关闭。快照新增keep_open和停杆位置。
