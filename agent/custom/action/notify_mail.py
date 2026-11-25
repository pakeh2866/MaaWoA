from __future__ import annotations

import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import pienv
from utils.logger import logger
from utils.params import parse_params

# SMTP 连接 / 收发超时（秒）。故障路径本来就要等待，这里不做异步，避免进程退出时丢失邮件。
_SMTP_TIMEOUT = 8.0

# 同一进程内的节流状态：连续故障只提醒一次，避免每轮识别失败都发一封。
_LOCK = threading.Lock()
_last_sent_at: float = 0.0


def _clean(value: object) -> str:
    """把 JSON 反序列化出来的值收敛成去空白的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _build_body(interval_minutes: int, is_test: bool = False) -> str:
    client = pienv.client_name() or "-"
    controller = pienv.controller_type() or "-"
    if is_test:
        head = [
            "MaaWoA 邮件提醒测试。",
            "",
            "如果你收到这封邮件，说明 SMTP 服务器、端口和授权码都可用了。",
            "",
        ]
        tail = ["", "这是一封由「📧发送测试邮件」任务触发的测试邮件，请勿直接回复。"]
    else:
        head = [
            "MaaWoA 无人值守模式出现异常，已进入故障恢复流程（尝试返回 / 重启游戏）。",
            "",
        ]
        tail = [
            "",
            f"同一故障 {interval_minutes} 分钟内只提醒一次；若恢复后再次异常会重新发送。",
            "本邮件由 MaaWoA 自动发送，请勿直接回复。",
        ]
    return "\n".join(
        [
            *head,
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"客户端：{client}",
            f"控制器：{controller}",
            *tail,
        ]
    )


def _send_mail(
    host: str,
    port: int,
    user: str,
    password: str,
    to: str,
    subject: str,
    body: str,
) -> None:
    message = EmailMessage()
    message["From"] = user
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)

    ssl_context = ssl.create_default_context()
    client: smtplib.SMTP
    if port == 465:
        # 465 为隐式 SSL；587 走 STARTTLS（QQ / 163 / 企业邮箱的通用配置）
        client = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT, context=ssl_context)
    else:
        client = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)

    try:
        if port != 465:
            client.ehlo()
            client.starttls(context=ssl_context)
            client.ehlo()
        client.login(user, password)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except (smtplib.SMTPException, OSError):
            pass


@AgentServer.custom_action("notify_mail")
class NotifyMail(CustomAction):
    """任务进入故障恢复流程时发送一封提醒邮件。

    custom_action_param:
        {
            "host": "smtp.qq.com",   // SMTP 服务器
            "port": 587,             // 587=STARTTLS, 465=SSL
            "user": "xxx@qq.com",    // 发件邮箱（登录账号）
            "password": "授权码",     // 邮箱授权码，不是登录密码
            "to": "xxx@qq.com",      // 收件人
            "subject": "MaaWoA 无人值守中断",
            "min_interval": 10       // 最短发送间隔（分钟）
        }

    参数不完整（用户没在设置里配邮件）时静默跳过，不影响故障恢复流程。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        global _last_sent_at

        try:
            params: dict[str, Any] = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("notify_mail: 参数解析失败，跳过本次提醒: {}", error)
            return CustomAction.RunResult(success=True)

        host = _clean(params.get("host"))
        user = _clean(params.get("user"))
        to = _clean(params.get("to"))
        password = _clean(params.get("password"))
        subject = _clean(params.get("subject")) or "MaaWoA 无人值守中断"
        port = _as_int(params.get("port"), 587)
        interval = max(_as_int(params.get("min_interval"), 10), 1)
        is_test = _clean(params.get("force")).lower() in {"1", "true", "yes"}

        if not host or not user or not to or not password:
            logger.warning("notify_mail: 邮件参数不完整，跳过本次提醒")
            return CustomAction.RunResult(success=not is_test)

        if not is_test:
            with _LOCK:
                now = time.monotonic()
                if _last_sent_at and now - _last_sent_at < interval * 60:
                    logger.debug("notify_mail: {} 分钟内已提醒过，跳过", interval)
                    return CustomAction.RunResult(success=True)
                _last_sent_at = now

        try:
            _send_mail(host, port, user, password, to, subject, _build_body(interval, is_test))
        except Exception as error:  # 正常提醒时邮件失败绝不能打断恢复流程；测试时返回失败以便界面提示
            with _LOCK:
                _last_sent_at = 0.0  # 失败不占用节流窗口，下次故障可重试
            logger.error("notify_mail: 发送失败 {}", error)
            return CustomAction.RunResult(success=not is_test)

        logger.info("notify_mail: 已向 {} 发送{}", to, "测试邮件" if is_test else "提醒邮件")
        return CustomAction.RunResult(success=True)
