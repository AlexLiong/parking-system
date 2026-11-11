"""Single-process demo clock. Uses the same lock as barrier actions and DB writes.

实现 B：演示模式业务时间统一走本时钟。``sim_time`` 为会话内仿真秒，``business_now()``
返回 ``epoch + sim_time``（暂停冻结、倍速线性、reset 归零）；真实墙钟仍用于 created_at
等审计字段。速度范围 0.25–10（后端校验）。
"""
import time
from datetime import timedelta

from .errors import StateConflict, ValidationError
from .timeutil import iso_local, utcnow

SPEED_MIN = 0.25
SPEED_MAX = 10.0


class SimulationClock:
    def __init__(self, container):
        self.c=container
        self.paused=False
        self.speed=1.0
        self.sim_time=0.0
        self.revision=0
        self._last=time.monotonic()
        self._paused_started=None
        # 业务时间基准：演示模式下业务时间 = epoch + sim_time（跑得越快业务时间越快）。
        self.epoch=utcnow()

    def business_now(self):
        """业务时间：演示模式 = epoch + sim_time（暂停/倍速/重置均生效）；否则真实墙钟。"""
        if not self.c.cfg.server.demo_mode:
            return utcnow()
        return self.epoch + timedelta(seconds=float(self.sim_time))

    def view(self):
        return {'paused':self.paused,'speed':self.speed,'sim_time':self.sim_time,
                'revision':self.revision,'enabled':self.c.cfg.server.demo_mode,
                'business_time':iso_local(self.business_now()),
                'speed_min':SPEED_MIN,'speed_max':SPEED_MAX,
                'epoch':iso_local(self.epoch)}

    def tick(self):
        with self.c.db.write_lock:
            now=time.monotonic()
            elapsed=max(0,now-self._last)
            self._last=now
            dt=0 if self.paused else elapsed*self.speed
            self.sim_time+=dt
            if dt:self.c.access.tick(dt)

    def control(self, *, paused=None, speed=None):
        if not self.c.cfg.server.demo_mode:
            raise StateConflict('demo_disabled','仅显式演示模式支持暂停/调速')
        if speed is not None:
            try:
                speed = float(speed)
            except (TypeError, ValueError) as exc:
                raise ValidationError('速度必须为数字') from exc
            if not SPEED_MIN <= speed <= SPEED_MAX:
                raise ValidationError(f'速度应在 {SPEED_MIN:g}–{SPEED_MAX:g} 之间')
        from .events import SimulationChanged
        with self.c.db.write_lock:
            self.tick()
            was_paused = self.paused
            if paused is not None:
                self.paused = paused
            # P1-05：暂停期间 pending 倒计时冻结；恢复时把暂停时长顺延给待办。
            now = time.monotonic()
            if self.paused and not was_paused:
                self._paused_started = now
            elif was_paused and not self.paused and self._paused_started is not None:
                paused_seconds = max(0.0, now - self._paused_started)
                self._paused_started = None
                try:
                    self.c.access.shift_pending_deadlines(paused_seconds)
                except Exception:
                    pass
            if speed is not None:self.speed=speed
            self.revision+=1
            self._last=now
            result=self.view()
            self.c.bus.publish(SimulationChanged(**result))
            return result

    def reset(self):
        with self.c.db.write_lock:
            self.paused=False;self.speed=1;self.sim_time=0;self.revision+=1
            self._last=time.monotonic();self._paused_started=None
            self.epoch=utcnow()
