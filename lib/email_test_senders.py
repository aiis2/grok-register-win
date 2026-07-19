#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-recipient sender strategies for end-to-end mailbox receive tests."""

from __future__ import annotations

import re
import smtplib
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from typing import Protocol


@dataclass(frozen=True)
class SenderCapability:
    mode: str
    available: bool
    reason: str = ""


class TestSender(Protocol):
    mode: str

    def capability(self) -> SenderCapability: ...

    def send(self, *, recipient: str, code: str) -> dict: ...


class SenderUnavailableError(RuntimeError):
    """No configured delivery strategy can perform the requested test."""


def _valid_address(value: str) -> bool:
    address = str(value or "").strip()
    if not address or "\r" in address or "\n" in address or address.count("@") != 1:
        return False
    local, domain = address.rsplit("@", 1)
    return bool(local and domain and " " not in address and "." in domain)


def _test_message(*, sender: str, recipient: str, code: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "Grok verification code"
    message.set_content(f"Verification code: {code}\n")
    return message


def _safe_exception(exc: BaseException) -> str:
    text = str(exc or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"(?i)(password|token|authorization)\s*[=:]\s*\S+", r"\1=[redacted]", text)
    return text[:240] or type(exc).__name__


def _smtp_constructor(factory, kind: str):
    if factory is None:
        return smtplib.SMTP_SSL if kind == "ssl" else smtplib.SMTP
    if isinstance(factory, dict):
        return factory[kind]
    name = "SMTP_SSL" if kind == "ssl" else "SMTP"
    constructor = getattr(factory, name, None)
    if callable(constructor):
        return constructor
    if callable(factory):
        return factory
    raise TypeError("无效 SMTP 工厂")


class FreemailNativeSender:
    mode = "native"

    def __init__(self, config: dict, mailbox):
        self.config = dict(config or {})
        self.mailbox = mailbox

    def capability(self) -> SenderCapability:
        probe = getattr(self.mailbox, "probe_send_capability", None)
        if not callable(probe):
            return SenderCapability(self.mode, False, "原生发件接口不可用")
        try:
            result = probe() or {}
        except Exception as exc:
            return SenderCapability(
                self.mode, False, f"Freemail 能力探测失败: {_safe_exception(exc)}"
            )
        return SenderCapability(
            self.mode,
            bool(result.get("available")),
            str(result.get("reason") or ""),
        )

    def send(self, *, recipient: str, code: str) -> dict:
        if not _valid_address(recipient):
            raise ValueError("测试收件地址无效")
        configured_from = str(self.config.get("mail_test_smtp_from") or "").strip()
        sender = configured_from if _valid_address(configured_from) else recipient
        result = self.mailbox.send_test_message(
            sender=sender,
            recipient=recipient,
            subject="Grok verification code",
            text=f"Verification code: {code}",
        )
        safe = {
            "mode": self.mode,
            "success": bool((result or {}).get("success", True)),
        }
        for key in ("provider", "id"):
            if (result or {}).get(key):
                safe[key] = str(result[key])
        return safe


class SmtpRelayTestSender:
    mode = "smtp"

    def __init__(self, config: dict, *, smtp_factory=None):
        self.config = dict(config or {})
        self.smtp_factory = smtp_factory

    def capability(self) -> SenderCapability:
        host = str(self.config.get("mail_test_smtp_host") or "").strip()
        sender = str(self.config.get("mail_test_smtp_from") or "").strip()
        username = str(self.config.get("mail_test_smtp_username") or "").strip()
        password = str(self.config.get("mail_test_smtp_password") or "").strip()
        if not host:
            return SenderCapability(self.mode, False, "SMTP 主机未配置")
        if not _valid_address(sender):
            return SenderCapability(self.mode, False, "SMTP 发件地址未配置或无效")
        if bool(username) != bool(password):
            return SenderCapability(self.mode, False, "SMTP 用户名和密码必须同时配置")
        security = str(
            self.config.get("mail_test_smtp_security") or "starttls"
        ).strip().lower()
        if security not in ("ssl", "starttls", "plain"):
            return SenderCapability(self.mode, False, "SMTP 安全模式无效")
        try:
            port = int(self.config.get("mail_test_smtp_port") or 587)
        except (TypeError, ValueError):
            return SenderCapability(self.mode, False, "SMTP 端口无效")
        if not 1 <= port <= 65535:
            return SenderCapability(self.mode, False, "SMTP 端口无效")
        return SenderCapability(self.mode, True, "")

    def send(self, *, recipient: str, code: str) -> dict:
        capability = self.capability()
        if not capability.available:
            raise SenderUnavailableError(capability.reason)
        if not _valid_address(recipient):
            raise ValueError("测试收件地址无效")

        host = str(self.config["mail_test_smtp_host"]).strip()
        port = int(self.config.get("mail_test_smtp_port") or 587)
        security = str(
            self.config.get("mail_test_smtp_security") or "starttls"
        ).strip().lower()
        username = str(self.config.get("mail_test_smtp_username") or "").strip()
        password = str(self.config.get("mail_test_smtp_password") or "").strip()
        sender = str(self.config["mail_test_smtp_from"]).strip()
        timeout = min(30, max(3, int(self.config.get("mail_test_timeout_sec") or 30)))
        client = None
        try:
            kind = "ssl" if security == "ssl" else "smtp"
            client = _smtp_constructor(self.smtp_factory, kind)(
                host, port, timeout=timeout
            )
            if security != "ssl":
                client.ehlo()
            if security == "starttls":
                client.starttls()
                client.ehlo()
            if username and password:
                client.login(username, password)
            message = _test_message(
                sender=sender, recipient=recipient, code=str(code)
            )
            refused = client.send_message(
                message,
                from_addr=sender,
                to_addrs=[recipient],
            )
            if refused:
                raise RuntimeError("SMTP 服务拒绝测试收件地址")
            return {"mode": self.mode, "success": True}
        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(f"SMTP 认证失败 (HTTP-like code {exc.smtp_code})") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise RuntimeError("SMTP 服务拒绝测试收件地址") from exc
        except (smtplib.SMTPConnectError, ConnectionError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"SMTP 连接失败: {_safe_exception(exc)}") from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise RuntimeError(f"SMTP TLS/认证能力不支持: {_safe_exception(exc)}") from exc
        except smtplib.SMTPException as exc:
            raise RuntimeError(f"SMTP 发件失败: {_safe_exception(exc)}") from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass


class DirectMxTestSender:
    mode = "direct_mx"

    def __init__(self, config: dict, *, smtp_factory=None, resolver=None):
        self.config = dict(config or {})
        self.smtp_factory = smtp_factory
        self.resolver = resolver

    def capability(self) -> SenderCapability:
        if not bool(self.config.get("mail_test_direct_mx_enabled", False)):
            return SenderCapability(self.mode, False, "Direct MX 未启用")
        return SenderCapability(self.mode, True, "")

    def _resolver(self):
        if self.resolver is not None:
            return self.resolver
        import dns.resolver

        return dns.resolver

    def send(self, *, recipient: str, code: str) -> dict:
        capability = self.capability()
        if not capability.available:
            raise SenderUnavailableError(capability.reason)
        if not _valid_address(recipient):
            raise ValueError("测试收件地址无效")
        domain = recipient.rsplit("@", 1)[1].lower()
        try:
            records = list(self._resolver().resolve(domain, "MX"))
        except Exception as exc:
            raise RuntimeError(f"Direct MX 查询失败: {_safe_exception(exc)}") from exc
        hosts = sorted(
            (
                int(getattr(record, "preference")),
                str(getattr(record, "exchange")).rstrip("."),
            )
            for record in records
        )
        if not hosts:
            raise RuntimeError("Direct MX 查询未返回服务器")

        configured_from = str(self.config.get("mail_test_smtp_from") or "").strip()
        envelope_from = configured_from if _valid_address(configured_from) else ""
        header_from = (
            configured_from
            if envelope_from
            else f"Grok Register Mail Test <postmaster@{domain}>"
        )
        message = _test_message(
            sender=header_from, recipient=recipient, code=str(code)
        ).as_string()
        errors = []
        for _, host in hosts:
            client = None
            try:
                client = _smtp_constructor(self.smtp_factory, "smtp")(
                    host, 25, timeout=30
                )
                code_value, _ = client.ehlo()
                if not 200 <= int(code_value) < 400:
                    raise RuntimeError(f"EHLO {code_value}")
                if bool(client.has_extn("starttls")):
                    client.starttls()
                    client.ehlo()
                code_value, _ = client.mail(envelope_from)
                if not 200 <= int(code_value) < 300:
                    raise RuntimeError(f"MAIL FROM {code_value}")
                code_value, _ = client.rcpt(recipient)
                if not 200 <= int(code_value) < 300:
                    raise RuntimeError(f"RCPT TO {code_value}")
                code_value, _ = client.data(message)
                if not 200 <= int(code_value) < 300:
                    raise RuntimeError(f"DATA {code_value}")
                return {"mode": self.mode, "success": True, "mx_host": host}
            except Exception as exc:
                errors.append(f"{host}: {_safe_exception(exc)}")
            finally:
                if client is not None:
                    try:
                        client.quit()
                    except Exception:
                        pass
        raise RuntimeError("Direct MX 所有服务器均投递失败: " + "; ".join(errors))


NATIVE_SENDER_FACTORIES = {
    "freemail": lambda config, mailbox: FreemailNativeSender(config, mailbox),
}


def _candidate_senders(
    config: dict,
    provider: str,
    mailbox,
    *,
    smtp_factory=None,
    resolver=None,
):
    native_factory = NATIVE_SENDER_FACTORIES.get(str(provider or "").strip().lower())
    native = (
        native_factory(config, mailbox)
        if native_factory
        else _UnsupportedNativeSender(str(provider or ""))
    )
    return {
        "native": native,
        "smtp": SmtpRelayTestSender(config, smtp_factory=smtp_factory),
        "direct_mx": DirectMxTestSender(
            config, smtp_factory=smtp_factory, resolver=resolver
        ),
    }


class _UnsupportedNativeSender:
    mode = "native"

    def __init__(self, provider: str):
        self.provider = provider

    def capability(self) -> SenderCapability:
        label = self.provider or "当前邮箱源"
        return SenderCapability(self.mode, False, f"{label} 不支持原生发件")

    def send(self, *, recipient: str, code: str) -> dict:
        raise SenderUnavailableError(self.capability().reason)


def sender_capabilities(
    config: dict,
    provider: str,
    mailbox,
    *,
    smtp_factory=None,
    resolver=None,
) -> list[dict]:
    senders = _candidate_senders(
        config,
        provider,
        mailbox,
        smtp_factory=smtp_factory,
        resolver=resolver,
    )
    return [asdict(senders[mode].capability()) for mode in ("native", "smtp", "direct_mx")]


def choose_test_sender(
    config: dict,
    provider: str,
    mailbox,
    *,
    smtp_factory=None,
    resolver=None,
):
    senders = _candidate_senders(
        config,
        provider,
        mailbox,
        smtp_factory=smtp_factory,
        resolver=resolver,
    )
    raw_mode = str((config or {}).get("mail_test_sender_mode") or "auto").strip().lower()
    aliases = {
        "provider_native": "native",
        "smtp_relay": "smtp",
    }
    mode = aliases.get(raw_mode, raw_mode)
    if mode != "auto":
        if mode not in senders:
            raise SenderUnavailableError(f"未知测试发件模式: {raw_mode}")
        capability = senders[mode].capability()
        if not capability.available:
            raise SenderUnavailableError(capability.reason)
        return senders[mode]

    reasons = []
    for candidate_mode in ("native", "smtp", "direct_mx"):
        capability = senders[candidate_mode].capability()
        if capability.available:
            return senders[candidate_mode]
        label = {
            "native": "原生 API",
            "smtp": "SMTP",
            "direct_mx": "Direct MX",
        }[candidate_mode]
        reasons.append(f"{label}: {capability.reason}")
    raise SenderUnavailableError("；".join(reasons))
