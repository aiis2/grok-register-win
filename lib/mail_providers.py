#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Multi mailbox provider adapter (any-auto-register base_mailbox).

Used by grok_register_ttk to support dropdown providers:
  cfworker, cloudflare_temp_email, moemail, tempmail_lol, duckmail, gptmail,
  maliapi, luckmail, skymail, cloudmail, freemail, opentrashmail, laoudo
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from mailbox_core import GROK_CODE_PATTERN, MailboxAccount as CoreMailboxAccount
except Exception:
    GROK_CODE_PATTERN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"
    CoreMailboxAccount = None

try:
    import base_mailbox as bm

    _IMPORT_OK = True
    _IMPORT_ERR = ""
except Exception as exc:
    bm = None
    _IMPORT_OK = False
    _IMPORT_ERR = str(exc)

MAIL_PROVIDER_CHOICES = [
    ("cfworker", "CF Worker / 自建域名"),
    ("cloudflare_temp_email", "Cloudflare Temp Email / 自建域名"),
    ("moemail", "MoeMail (sall.cc)"),
    ("tempmail_lol", "TempMail.lol（自动生成）"),
    ("duckmail", "DuckMail"),
    ("gptmail", "GPTMail"),
    ("maliapi", "YYDS / MaliAPI"),
    ("luckmail", "LuckMail（接码/买邮）"),
    ("skymail", "SkyMail"),
    ("cloudmail", "CloudMail"),
    ("freemail", "Freemail 自建"),
    ("opentrashmail", "OpenTrashMail"),
    ("laoudo", "Laoudo 固定邮箱"),
]

ENV_FALLBACKS = {
    "freemail_api_url": "MAIL_WEB_URL",
    "freemail_username": "ADMIN_NAME",
    "freemail_password": "ADMIN_PASSWORD",
}

# active mailbox for wait_for_code
_ACTIVE_BOX = None
_ACTIVE_ACCT = None
_ACTIVE_PROVIDER = ""


def import_ok() -> bool:
    return bool(_IMPORT_OK and bm is not None)


def import_error() -> str:
    return _IMPORT_ERR


def normalize_provider(name: str) -> str:
    p = str(name or "").strip().lower()
    aliases = {
        "custom": "cfworker",
        "cloudflare": "cloudflare_temp_email",
        "cloudflare-temp-email": "cloudflare_temp_email",
        "cf-worker": "cfworker",
        "cf_worker": "cfworker",
        "tempmail": "tempmail_lol",
        "tempmail.lol": "tempmail_lol",
        "yyds": "maliapi",
        "yy ds": "maliapi",
    }
    if p in ("tempmailer", "inboxkitten", "inbox_kitten"):
        return "cfworker"
    return aliases.get(p, p or "cfworker")


def resolved_provider_config(config: dict, environ=None) -> dict:
    """Return a copy with supported environment fallbacks applied.

    Values explicitly stored in the application config always win.  The
    environment mapping is intentionally small so credentials never leak into
    config.json merely because the application used them.
    """
    resolved = dict(config or {})
    env = os.environ if environ is None else environ
    for config_key, environment_key in ENV_FALLBACKS.items():
        if not str(resolved.get(config_key) or "").strip():
            value = str(env.get(environment_key) or "").strip()
            if value:
                resolved[config_key] = value

    api_url = str(resolved.get("freemail_api_url") or "").strip()
    if api_url:
        if "://" not in api_url:
            api_url = f"https://{api_url}"
        resolved["freemail_api_url"] = api_url.rstrip("/")
    for key in ("freemail_username", "freemail_password"):
        if key in resolved:
            resolved[key] = str(resolved.get(key) or "").strip()
    return resolved


def extra_from_config(config: dict) -> dict:
    c = resolved_provider_config(config)
    cf_url = str(c.get("cfworker_api_url") or "").strip()
    cf_token = str(c.get("cfworker_admin_token") or "").strip()
    cf_domain = str(c.get("cfworker_domain") or "").strip()
    return {
        "cloudflare_api_base": str(c.get("cloudflare_api_base") or "").strip(),
        "cloudflare_admin_password": str(
            c.get("cloudflare_admin_password") or c.get("cloudflare_api_key") or ""
        ).strip(),
        "cloudflare_domain": str(
            c.get("cloudflare_domain") or c.get("defaultDomains") or ""
        ).strip(),
        "cloudflare_site_password": str(
            c.get("cloudflare_site_password") or c.get("cfworker_custom_auth") or ""
        ).strip(),
        "moemail_api_url": str(c.get("moemail_api_url") or "https://sall.cc").strip(),
        "moemail_api_key": str(c.get("moemail_api_key") or "").strip(),
        "skymail_api_base": str(c.get("skymail_api_base") or "https://api.skymail.ink").strip(),
        "skymail_token": str(c.get("skymail_token") or "").strip(),
        "skymail_domain": str(c.get("skymail_domain") or "").strip(),
        "cloudmail_api_base": str(c.get("cloudmail_api_base") or "").strip(),
        "cloudmail_admin_email": str(c.get("cloudmail_admin_email") or "").strip(),
        "cloudmail_admin_password": str(
            c.get("cloudmail_admin_password") or c.get("cloudflare_api_key") or ""
        ).strip(),
        "cloudmail_domain": str(c.get("cloudmail_domain") or c.get("defaultDomains") or "").strip(),
        "duckmail_api_url": str(c.get("duckmail_api_url") or "https://www.duckmail.sbs").strip(),
        "duckmail_provider_url": str(
            c.get("duckmail_provider_url") or "https://api.duckmail.sbs"
        ).strip(),
        "duckmail_bearer": str(c.get("duckmail_bearer") or "").strip(),
        "duckmail_domain": str(c.get("duckmail_domain") or "").strip(),
        "duckmail_api_key": str(c.get("duckmail_api_key") or "").strip(),
        "freemail_api_url": str(c.get("freemail_api_url") or "").strip(),
        "freemail_admin_token": str(c.get("freemail_admin_token") or "").strip(),
        "freemail_username": str(c.get("freemail_username") or "").strip(),
        "freemail_password": str(c.get("freemail_password") or "").strip(),
        "freemail_domain": str(c.get("freemail_domain") or "").strip(),
        "maliapi_base_url": str(c.get("maliapi_base_url") or "https://maliapi.215.im/v1").strip(),
        "maliapi_api_key": str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip(),
        "maliapi_domain": str(c.get("maliapi_domain") or "").strip(),
        "maliapi_auto_domain_strategy": str(c.get("maliapi_auto_domain_strategy") or "").strip(),
        "gptmail_base_url": str(c.get("gptmail_base_url") or "https://mail.chatgpt.org.uk").strip(),
        "gptmail_api_key": str(c.get("gptmail_api_key") or "").strip(),
        "gptmail_domain": str(c.get("gptmail_domain") or "").strip(),
        "opentrashmail_api_url": str(c.get("opentrashmail_api_url") or "").strip(),
        "opentrashmail_domain": str(c.get("opentrashmail_domain") or "").strip(),
        "opentrashmail_password": str(c.get("opentrashmail_password") or "").strip(),
        "cfworker_api_url": cf_url,
        "cfworker_admin_token": cf_token,
        "cfworker_domain": cf_domain,
        "cfworker_domain_override": str(c.get("cfworker_domain_override") or "").strip(),
        "cfworker_custom_auth": str(c.get("cfworker_custom_auth") or "").strip(),
        "cfworker_subdomain": str(c.get("cfworker_subdomain") or "").strip(),
        "cfworker_fingerprint": str(c.get("cfworker_fingerprint") or "").strip(),
        "luckmail_base_url": str(c.get("luckmail_base_url") or "https://mails.luckyous.com/").strip(),
        "luckmail_api_key": str(c.get("luckmail_api_key") or "").strip(),
        "luckmail_project_code": str(c.get("luckmail_project_code") or "grok").strip(),
        "luckmail_email_type": str(c.get("luckmail_email_type") or "").strip(),
        "luckmail_domain": str(c.get("luckmail_domain") or "").strip(),
        "laoudo_auth": str(c.get("laoudo_auth") or "").strip(),
        "laoudo_email": str(c.get("laoudo_email") or "").strip(),
        "laoudo_account_id": str(c.get("laoudo_account_id") or "").strip(),
    }


def provider_ready(config: dict, provider: str) -> bool:
    c = resolved_provider_config(config)
    p = normalize_provider(provider)
    if p in ("tempmailer", "inboxkitten", "inbox_kitten"):
        return False
    if p in ("tempmail_lol", "moemail", "gptmail", "duckmail"):
        return True
    if p == "maliapi":
        return bool(
            str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip()
            or str(c.get("yyds_jwt") or "").strip()
        )
    if p == "luckmail":
        return bool(str(c.get("luckmail_api_key") or "").strip())
    if p == "skymail":
        return bool(str(c.get("skymail_token") or "").strip())
    if p == "cloudmail":
        return bool(str(c.get("cloudmail_api_base") or "").strip())
    if p == "freemail":
        return bool(str(c.get("freemail_api_url") or "").strip())
    if p == "opentrashmail":
        return bool(str(c.get("opentrashmail_api_url") or "").strip())
    if p == "laoudo":
        return bool(str(c.get("laoudo_email") or "").strip())
    if p == "cloudflare_temp_email":
        extra = extra_from_config(c)
        return all(
            extra[key]
            for key in (
                "cloudflare_api_base",
                "cloudflare_admin_password",
                "cloudflare_domain",
            )
        )
    if p == "cfworker":
        return bool(str(c.get("cfworker_api_url") or "").strip())
    return False


def make_mailbox(config: dict, provider: str, proxy: str = "", log_callback=None):
    if not import_ok():
        raise RuntimeError(f"base_mailbox 未加载: {_IMPORT_ERR}")
    prov = normalize_provider(provider)
    extra = extra_from_config(config)
    box = bm.create_mailbox(prov, extra=extra, proxy=proxy or None)
    try:
        box._log_fn = log_callback
    except Exception:
        pass
    if log_callback:
        log_callback(f"[*] 邮箱适配器: {prov}")
    return box, prov


def get_email_and_token(
    config: dict,
    provider: str,
    proxy: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, str]:
    global _ACTIVE_BOX, _ACTIVE_ACCT, _ACTIVE_PROVIDER
    box, prov = make_mailbox(config, provider, proxy=proxy, log_callback=log_callback)
    acct = box.get_email()
    email = str(getattr(acct, "email", "") or "").strip()
    token = str(getattr(acct, "account_id", "") or "").strip() or email
    extra = getattr(acct, "extra", None) or {}
    if isinstance(extra, dict):
        for k in ("jwt", "token", "session", "auth"):
            if extra.get(k):
                token = str(extra.get(k)).strip()
                break
    if not email:
        raise RuntimeError(f"{prov} 返回空邮箱")
    _ACTIVE_BOX = box
    _ACTIVE_ACCT = acct
    _ACTIVE_PROVIDER = prov
    if log_callback:
        log_callback(f"[*] 已申请邮箱: {email}（源={prov}）")
    return email, token


def cleanup_active_mailbox(log_callback=None) -> bool:
    """Delete a dedicated Cloudflare address and always forget active state."""
    global _ACTIVE_BOX, _ACTIVE_ACCT, _ACTIVE_PROVIDER
    box = _ACTIVE_BOX
    account = _ACTIVE_ACCT
    provider = _ACTIVE_PROVIDER
    _ACTIVE_BOX = None
    _ACTIVE_ACCT = None
    _ACTIVE_PROVIDER = ""
    if provider != "cloudflare_temp_email" or box is None or account is None:
        return True
    delete_email = getattr(box, "delete_email", None)
    if not callable(delete_email):
        return True
    try:
        deleted = bool(delete_email(account))
        if log_callback:
            if deleted:
                log_callback("[*] Cloudflare Temp Email 临时地址已清理")
            else:
                log_callback("[!] Cloudflare Temp Email 临时地址未能清理（已忽略）")
        return deleted
    except Exception:
        if log_callback:
            log_callback("[!] Cloudflare Temp Email 临时地址清理失败（已忽略）")
        return False


def snapshot_ids(log_callback=None):
    global _ACTIVE_BOX, _ACTIVE_ACCT
    if _ACTIVE_BOX is None or _ACTIVE_ACCT is None:
        return set()
    try:
        ids = _ACTIVE_BOX.get_current_ids(_ACTIVE_ACCT) or set()
        ids = {str(x) for x in ids if x}
        if log_callback and ids:
            log_callback(f"[*] 发码前收件箱已有 {len(ids)} 封邮件，将忽略旧信")
        return ids
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 快照收件箱 id 失败（可忽略）: {exc}")
        return set()


def wait_for_code(
    email: str,
    dev_token: str,
    *,
    timeout: int = 180,
    cancel_callback: Optional[Callable[[], bool]] = None,
    before_ids=None,
    otp_sent_at=None,
    log_callback: Optional[Callable[[str], None]] = None,
    config: Optional[dict] = None,
    provider: str = "",
    proxy: str = "",
) -> str:
    global _ACTIVE_BOX, _ACTIVE_ACCT
    box = _ACTIVE_BOX
    acct = _ACTIVE_ACCT
    if box is None:
        box, _ = make_mailbox(config or {}, provider or "cfworker", proxy=proxy, log_callback=log_callback)
    if acct is None:
        acct = bm.MailboxAccount(email=email, account_id=dev_token or email)

    class _Ctl:
        def checkpoint(self, **kwargs):
            if cancel_callback and cancel_callback():
                raise RuntimeError("用户停止注册")

    try:
        box._task_control = _Ctl()
    except Exception:
        pass

    code = box.wait_for_code(
        acct,
        keyword="",
        timeout=int(timeout or 180),
        before_ids=set(before_ids or set()),
        code_pattern=GROK_CODE_PATTERN,
        otp_sent_at=otp_sent_at,
    )
    if not code:
        raise RuntimeError("未收到验证码")
    if log_callback:
        log_callback(f"[*] 验证码: {code}")
    return str(code).strip()
