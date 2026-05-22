#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
from urllib import request

from .config import AppConfig

LOGGER = logging.getLogger("mass_dashboard.notifier")


def send_notification(config: AppConfig, title: str, text: str) -> bool:
    if not config.alert_webhook_url:
        return False

    if config.alert_webhook_type.lower() == "generic":
        payload = {"title": title, "text": text}
    else:
        payload = {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        config.alert_webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as err:
        LOGGER.warning("发送告警通知失败: %s", err)
        return False

