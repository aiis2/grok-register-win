#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transactional orchestration for end-to-end mailbox receive testing."""

from __future__ import annotations

import hmac
import re
import secrets
import string
import time
from typing import Callable, Optional

import mail_providers
from email_test_senders import choose_test_sender
from mailbox_core import GROK_CODE_PATTERN


class ReceiveTestError(RuntimeError):
    def __init__(self, stage: str, message: str, details=None):
        self.stage = str(stage or "checking")
        self.message = str(message or "邮箱收件测试失败")
        self.details = details
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


class ReceiveTestCancelled(ReceiveTestError):
    pass


def mask_email(address: str) -> str:
    value = str(address or "").strip()
    if "@" not in value:
        return "***"
    local, domain = value.rsplit("@", 1)
    prefix = local[:1] if local else ""
    return f"{prefix}***@{domain}"


def sanitize_receive_test_error(
    error,
    *,
    config: Optional[dict] = None,
    email: str = "",
    limit: int = 500,
) -> str:
    """Redact secrets, mailbox identity and Grok-shaped codes from errors."""
    text = str(error or "邮箱收件测试失败").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(cookie|set-cookie|password|passwd|token|authorization|api[_-]?key)"
        r"\s*[=:]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    for key, value in (config or {}).items():
        lowered = str(key).lower()
        if not any(
            marker in lowered
            for marker in ("password", "passwd", "token", "secret", "cookie", "api_key")
        ):
            continue
        secret = str(value or "")
        if len(secret) >= 3:
            text = text.replace(secret, "[redacted]")
    address = str(email or "").strip()
    if address:
        text = text.replace(address, mask_email(address))
        local = address.rsplit("@", 1)[0]
        if len(local) >= 3:
            text = text.replace(local, "***")
    text = re.sub(r"\b[A-Z0-9]{3}-[A-Z0-9]{3}\b", "[code]", text)
    return re.sub(r"\s+", " ", text).strip()[: max(80, int(limit))]


def _random_grok_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(3)) + "-" + "".join(
        secrets.choice(alphabet) for _ in range(3)
    )


def run_email_receive_test(
    config: dict,
    *,
    on_stage: Callable[[str, dict], None],
    cancelled: Callable[[], bool],
    mailbox_factory=mail_providers.make_mailbox,
    sender_selector=choose_test_sender,
    code_factory=None,
    monotonic=time.monotonic,
) -> dict:
    """Create, send to, receive from, verify, and clean one test mailbox."""
    private_config = mail_providers.resolved_provider_config(dict(config or {}))
    configured_provider = mail_providers.normalize_provider(
        private_config.get("email_provider")
        or (private_config.get("email_providers") or ["cfworker"])[0]
    )
    started_at = monotonic()
    receive_started = None
    receive_seconds = 0.0
    current_stage = "checking"
    mailbox = None
    account = None
    sender = None
    provider = configured_provider
    email = ""
    cleanup_status = "not_needed"
    warnings = []
    primary_error = None

    def emit(stage: str, detail=None, *, check_cancel=True):
        nonlocal current_stage
        current_stage = stage
        on_stage(stage, dict(detail or {}))
        if check_cancel and cancelled():
            raise ReceiveTestCancelled(stage, "邮箱收件测试已取消")

    try:
        emit("checking", {"provider": provider})
        if not mail_providers.provider_ready(private_config, provider):
            raise ReceiveTestError("checking", f"邮箱源 {provider} 配置不完整")
        mailbox, provider = mailbox_factory(
            private_config,
            provider,
            proxy=str(private_config.get("proxy") or ""),
        )
        sender = sender_selector(private_config, provider, mailbox)

        emit("creating", {"provider": provider, "sender_mode": sender.mode})
        account = mailbox.get_email()
        email = str(getattr(account, "email", "") or "").strip()
        if not email or "@" not in email:
            raise ReceiveTestError("creating", "邮箱服务返回了无效测试地址")
        safe_identity = {
            "provider": provider,
            "sender_mode": sender.mode,
            "email": mask_email(email),
        }

        emit("snapshotting", safe_identity)
        before_ids = {
            str(item)
            for item in (mailbox.get_current_ids(account) or set())
            if item is not None
        }

        test_code = str((code_factory or _random_grok_code)()).strip().upper()
        if not re.fullmatch(GROK_CODE_PATTERN, test_code):
            raise ReceiveTestError("sending", "内部生成的测试验证码格式无效")
        emit("sending", safe_identity)
        sender.send(recipient=email, code=test_code)

        emit("waiting", safe_identity)
        receive_started = monotonic()
        received_code = mailbox.wait_for_code(
            account,
            keyword="",
            timeout=max(15, min(300, int(private_config.get("mail_test_timeout_sec") or 90))),
            before_ids=before_ids,
            code_pattern=GROK_CODE_PATTERN,
        )
        receive_seconds = max(0.0, monotonic() - receive_started)
        received_code = str(received_code or "").strip().upper()
        if not received_code:
            raise ReceiveTestError("waiting", "超时，未收到测试验证码")

        emit("verifying", safe_identity)
        if not hmac.compare_digest(received_code, test_code):
            raise ReceiveTestError("verifying", "收到的验证码与本次测试不匹配")
    except ReceiveTestError as exc:
        primary_error = exc
    except Exception as exc:
        primary_error = ReceiveTestError(
            current_stage,
            sanitize_receive_test_error(exc, config=private_config, email=email),
        )

    if account is not None:
        current_stage = "cleaning"
        try:
            on_stage(
                "cleaning",
                {
                    "provider": provider,
                    "sender_mode": getattr(sender, "mode", ""),
                    "email": mask_email(email),
                },
            )
        except Exception as exc:
            if primary_error is None:
                primary_error = ReceiveTestError(
                    "cleaning",
                    sanitize_receive_test_error(exc, config=private_config, email=email),
                )
        cleanup = getattr(mailbox, "delete_email", None)
        if callable(cleanup):
            try:
                cleanup_status = "deleted" if bool(cleanup(account)) else "failed"
                if cleanup_status == "failed":
                    warnings.append("测试邮箱清理失败")
            except Exception as exc:
                cleanup_status = "failed"
                warnings.append(
                    "测试邮箱清理失败: "
                    + sanitize_receive_test_error(
                        exc, config=private_config, email=email, limit=240
                    )
                )
        else:
            cleanup_status = "unsupported"
        if primary_error is None and cancelled():
            primary_error = ReceiveTestCancelled("cleaning", "邮箱收件测试已取消")

    if primary_error is not None:
        if warnings:
            primary_error.message = sanitize_receive_test_error(
                f"{primary_error.message}；{'；'.join(warnings)}",
                config=private_config,
                email=email,
            )
        else:
            primary_error.message = sanitize_receive_test_error(
                primary_error.message, config=private_config, email=email
            )
        terminal = "cancelled" if isinstance(primary_error, ReceiveTestCancelled) else "failed"
        on_stage(
            terminal,
            {
                "provider": provider,
                "sender_mode": getattr(sender, "mode", ""),
                "email": mask_email(email) if email else "",
                "error": primary_error.message,
                "error_stage": primary_error.stage,
                "cleanup": cleanup_status,
            },
        )
        raise primary_error

    result = {
        "ok": True,
        "email": mask_email(email),
        "provider": provider,
        "sender_mode": getattr(sender, "mode", ""),
        "total_sec": round(max(0.0, monotonic() - started_at), 3),
        "receive_sec": round(receive_seconds, 3),
        "cleanup": cleanup_status,
        "warnings": warnings,
    }
    on_stage("succeeded", result)
    return result
