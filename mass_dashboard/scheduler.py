#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import AppConfig
from .pipeline import run_mass_pipeline

LOGGER = logging.getLogger("mass_dashboard.scheduler")


class DashboardScheduler:
    def __init__(self, config: AppConfig, check_interval_seconds: int = 60):
        self.config = config
        self.check_interval_seconds = check_interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._job_lock = threading.Lock()
        self._last_schedule_key = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._mark_today_seen_if_schedule_passed()
        self._thread = threading.Thread(target=self._loop, name="mass-dashboard-scheduler", daemon=True)
        self._thread.start()
        LOGGER.info("调度器已启动，每天 %s 触发", self.config.run_time)

    def stop(self) -> None:
        self._stop_event.set()

    def trigger_run(self, trade_date: Optional[str] = None, force: bool = False) -> tuple[bool, str]:
        if self._job_lock.locked():
            return False, "已有任务正在运行"

        thread = threading.Thread(
            target=self._run_guarded,
            args=(trade_date, force),
            name="mass-dashboard-manual-run",
            daemon=True,
        )
        thread.start()
        return True, "任务已启动"

    def _run_guarded(self, trade_date: Optional[str], force: bool, max_retries: int = 3) -> None:
        # 原子获取锁：acquire(blocking=False) 把"检查"和"获取"合并成一步，
        # 避免 trigger_run 的 locked() 预检查与这里的 acquire 之间的竞态。
        if not self._job_lock.acquire(blocking=False):
            return  # 被并发任务抢了，直接退出
        try:
            last_err = None
            for attempt in range(max_retries):
                try:
                    run_mass_pipeline(self.config, trade_date=trade_date, force=force)
                    return  # 成功则退出
                except Exception as err:
                    last_err = err
                    LOGGER.warning("任务运行失败(尝试 %s/%s): %s", attempt + 1, max_retries, err)
                    if attempt < max_retries - 1:
                        time.sleep(30)  # 等30秒重试
            # 全部重试失败
            LOGGER.error("任务运行最终失败(共尝试 %s 次): %s", max_retries, last_err)
        finally:
            self._job_lock.release()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._maybe_run_daily()
            except Exception as err:
                LOGGER.error("调度循环异常: %s", err)
            self._stop_event.wait(self.check_interval_seconds)

    def _maybe_run_daily(self) -> None:
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        hour, minute = [int(part) for part in self.config.run_time.split(":", 1)]
        schedule_key = f"{now:%Y%m%d}-{self.config.run_time}"
        if self._last_schedule_key == schedule_key:
            return
        if (now.hour, now.minute) < (hour, minute):
            return

        self._last_schedule_key = schedule_key
        ok, message = self.trigger_run(trade_date=None, force=False)
        LOGGER.info("每日调度触发: %s %s", ok, message)

    def _mark_today_seen_if_schedule_passed(self) -> None:
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        hour, minute = [int(part) for part in self.config.run_time.split(":", 1)]
        if (now.hour, now.minute) >= (hour, minute):
            self._last_schedule_key = f"{now:%Y%m%d}-{self.config.run_time}"
