#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Grok Register 账号面板 + 启动注册（代理/节点由本机 Clash 管理）"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

# Ensure stdout/stderr use UTF-8 on Windows (default is GBK/CP936)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from typing import Deque, Dict, List, Optional, Set, Tuple

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

# Project root = parent of panel/ (Windows / portable layout)
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("GROK_REGISTER_DIR", str(_DEFAULT_ROOT))).resolve()
# 面板默认不设登录密码（本机 127.0.0.1）。若需开启：PANEL_AUTH=1 且 PANEL_PASSWORD=xxx
PANEL_AUTH = os.environ.get("PANEL_AUTH", "0").strip() not in ("0", "false", "False", "no", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "admin")
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8787"))
SECRET = os.environ.get("PANEL_SECRET", "grok-register-panel-local-secret")
CLASH_API = os.environ.get("CLASH_API", "http://127.0.0.1:9090").rstrip("/")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "")
# Prefer project venv; Windows uses Scripts\python.exe
_VENV_WIN = BASE_DIR / ".venv" / "Scripts" / "python.exe"
_VENV_UNIX = BASE_DIR / ".venv" / "bin" / "python"
_DEFAULT_PY = (
    str(_VENV_WIN)
    if _VENV_WIN.exists()
    else (str(_VENV_UNIX) if _VENV_UNIX.exists() else sys.executable)
)
VENV_PYTHON = os.environ.get("GROK_PYTHON", _DEFAULT_PY)
MAIN_SCRIPT = BASE_DIR / "grok_register_ttk.py"
CONFIG_PATH = BASE_DIR / "config.json"
PROXY_URL = os.environ.get("GROK_PROXY", "http://127.0.0.1:7890")
LOG_DIR = Path(os.environ.get("PANEL_LOG_DIR", str(BASE_DIR / "data" / "logs"))).resolve()
LOG_DIR.mkdir(parents=True, exist_ok=True)

# SSO → real CPA (CLIProxyAPI OAuth JSON)
CPA_DIR = Path(os.environ.get("CPA_DIR", str(BASE_DIR / "data" / "cpa"))).resolve()
CPA_DIR.mkdir(parents=True, exist_ok=True)
CPA_INDEX_PATH = CPA_DIR / "index.json"
CPA_FAILED_PATH = CPA_DIR / "failed.jsonl"
SSO2CPA_PATH = Path(
    os.environ.get("SSO2CPA_PATH", str(BASE_DIR / "lib"))
).resolve()
AUTO_CPA = os.environ.get("AUTO_CPA", "1").strip() not in ("0", "false", "False", "no")
CPA_DELAY = float(os.environ.get("CPA_DELAY", "1.0"))
# Hard wall-clock per register round (one account). Stuck process is killed, next round starts.
DEFAULT_ROUND_TIMEOUT_SEC = 300
# Optional: talk to local Clash Meta external-controller for node list.
# Default: external Clash managed by user; node UI is best-effort.
ENABLE_CLASH_UI = os.environ.get("ENABLE_CLASH_UI", "1").strip() not in (
    "0",
    "false",
    "False",
    "no",
)

# import shared convert core
for _p in (str(SSO2CPA_PATH), str(BASE_DIR / "lib"), str(Path(__file__).resolve().parent)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from sso2cpa_core import (  # type: ignore
        build_sub2_payload,
        cpa_to_sub2_account,
        convert_one,
        normalize_sso,
        safe_filename as cpa_safe_filename,
        sso_fingerprint,
    )

    _CPA_CORE_OK = True
    _CPA_CORE_ERR = ""
except Exception as _e:  # pragma: no cover
    convert_one = None  # type: ignore
    build_sub2_payload = None  # type: ignore
    cpa_to_sub2_account = None  # type: ignore
    normalize_sso = lambda t: (t or "").strip()  # type: ignore
    cpa_safe_filename = lambda s: re.sub(r"[^\w.@+-]+", "_", s or "unknown")[:100]  # type: ignore
    sso_fingerprint = lambda s: hashlib.sha256((s or "").encode()).hexdigest()  # type: ignore
    _CPA_CORE_OK = False
    _CPA_CORE_ERR = str(_e)

HK_RE = re.compile(r"(香港|Hong\s*Kong|\bHK\b|🇭🇰)", re.I)

app = Flask(__name__)
app.secret_key = SECRET

# --------------- job state ---------------
_job_lock = threading.Lock()
_job: Dict = {
    "running": False,
    "stop": False,
    "pid": None,
    "started_at": None,
    "finished_at": None,
    "count": 0,
    "success": 0,
    "fail": 0,
    "current_round": 0,
    "current_node": "",
    "node_mode": "fixed",  # fixed | rotate_on_fail | rotate_each
    "node_list": [],
    "node_index": 0,
    "log_path": "",
    "last_error": "",
    "status": "idle",
}
_logs: Deque[str] = deque(maxlen=2000)
_proc: Optional[subprocess.Popen] = None

# --------------- CPA auto-convert queue ---------------
_cpa_lock = threading.Lock()
_cpa_q: "queue.Queue[Optional[dict]]" = queue.Queue()
_cpa_state: Dict = {
    "enabled": AUTO_CPA,
    "core_ok": _CPA_CORE_OK,
    "core_error": _CPA_CORE_ERR,
    "pending": 0,
    "ok": 0,
    "fail": 0,
    "running": False,
    "last_error": "",
    "last_ok_email": "",
}
_cpa_done: Set[str] = set()  # sso fingerprints already converted
_cpa_inflight: Set[str] = set()


def log_line(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _logs.append(line)
    path = _job.get("log_path")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# 日志过滤：只保留关键信息，屏蔽第三方库噪音
# 注意：不要把业务日志里的 Camoufox/Playwright 字样当噪音误杀
_LOG_NOISE_PATTERNS = re.compile(
    r"(?i)"
    r"(<html|<!doctype|<div|<script|<svg|<path\b)"          # HTML 片段
    r"|(?:^|\s)(?:playwright|drissionpage|selenium|urllib3)[\s:.]"  # 库调试，不含业务 Camoufox
    r"|(connection\.(reusable|pool)|starting new (http|https))"  # urllib3 连接日志
    r"|(\bDEBUG\b|\bTRACE\b)"                                # 调试级别
    r"|(node:|child_process|events\.js|node_modules)"        # Node.js 内部
    r"|(pip\s|Downloading\s|Installing collected)"           # pip 安装
)
_LOG_KEY_PREFIXES = ("[*]", "[+]", "[-]", "[!]", "[Debug]", "[i]", "[OK]", "[ERR]")
_LOG_KEY_KEYWORDS = (
    "注册成功", "注册失败", "任务结束", "任务异常", "浏览器已启动", "开始注册",
    "验证码", "邮箱", "NSFW", "CPA", "SSO", "OAuth", "账号", "停止", "清理",
    "成功账号", "当前统计", "保存", "失败", "成功", "启动", "结束",
    "浏览器", "Camoufox", "Chromium", "硬超时", "下载", "就绪",
)
# 噪音行模式（即使是 [*] 前缀也过滤）：Cloudflare 轮询、GC 回收、网络模式重复
_LOG_NOISE_LINES = re.compile(
    r"(?i)"
    r"(等待\s*Cloudflare\s*人机验证)"           # Cloudflare 轮询刷屏
    r"|(Cloudflare\s*token\s*为空.*继续检测)"    # Cloudflare token 空轮询
    r"|(Python\s*GC\s*已回收)"                  # GC 回收细节
    r"|(浏览器网络模式)"                        # 每轮重复的网络模式
    r"|(浏览器已启动)(?!.*\b第\b)"              # 第 N 轮以外的「浏览器已启动」重复
    r"|(邮箱源\s*\w+\s*创建成功)"               # 与「已创建邮箱」重复
    r"|(已创建邮箱.*源=)"                       # 与「已创建 tempmailer 邮箱」重复
    r"|(资料已填:)"                             # 与「已填写注册资料并提交」重复
    r"|(Turnstile\s*二次复用完成)"              # 调试细节
    r"|(提交前仍卡住.*复用\s*Turnstile)"        # 调试细节
)


def _strip_inner_timestamp(line: str) -> str:
    """去掉子进程日志自带的时间戳，避免与 panel 的 log_line 时间戳重复。
    子进程原始行形如 "[02:30:39] [*] CLI 已加载配置" → 去掉前导时间戳 → "[*] CLI 已加载配置"
    这样 log_line 再加时间戳就只有一层 "[02:30:39] [*] CLI 已加载配置"。
    """
    # 标准形式：[HH:MM:SS] 后跟内容
    m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s+(.*)$", line)
    if m:
        return m.group(1)
    # 带 > 前缀形式：> [HH:MM:SS] [*] xxx
    m = re.match(r"^>\s*\[\d{2}:\d{2}:\d{2}\]\s+(.*)$", line)
    if m:
        return m.group(1)
    return line


def _truncate_line(line: str, max_len: int = 200) -> str:
    """超长行截断，保留前部关键信息。"""
    if len(line) <= max_len:
        return line
    return line[:max_len] + " …"


def _is_key_log(line: str) -> bool:
    """判断一行日志是否为关键信息，应保留显示。"""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    # 超长单行通常是 URL 或 HTML 片段
    if len(stripped) > 400:
        return False
    # 即使带 [*] 前缀的噪音行也过滤（Cloudflare 轮询、GC、网络模式重复）
    if _LOG_NOISE_LINES.search(stripped):
        return False
    # 业务前缀优先保留（避免 “Camoufox/Playwright” 字样被整行误杀）
    for prefix in _LOG_KEY_PREFIXES:
        if prefix in stripped:
            return True
    # 噪音模式（无业务前缀时）
    if _LOG_NOISE_PATTERNS.search(stripped):
        return False
    # 关键业务关键词
    for kw in _LOG_KEY_KEYWORDS:
        if kw in stripped:
            return True
    # panel 自己写的 [!] 前缀日志（已带时间戳）
    if stripped.startswith("[") and "]" in stripped[:9]:
        rest = stripped[stripped.find("]") + 1 :].strip()
        if rest.startswith("[!]") or rest.startswith("[*]") or rest.startswith("[+]"):
            return True
    # 默认过滤（非关键噪音）
    return False


def require_login():
    """默认关闭鉴权；仅当 PANEL_AUTH=1 时校验 session。"""
    if not PANEL_AUTH:
        return None
    if session.get("ok"):
        return None
    # API requests get JSON 401
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return redirect(url_for("login", next=request.path))


def list_account_files() -> List[Path]:
    return sorted(
        BASE_DIR.glob("accounts_*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def read_account_lines(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def collect_all_accounts() -> List[Tuple[str, str]]:
    items = []
    for f in list_account_files():
        for line in read_account_lines(f):
            items.append((f.name, line))
    return items


def parse_line(line: str):
    parts = line.split("----")
    if len(parts) >= 3:
        return {
            "email": parts[0],
            "password": parts[1],
            "sso": "----".join(parts[2:]),
            "raw": line,
        }
    return {"email": line, "password": "", "sso": "", "raw": line}


def _b64url_json(segment: str):
    import base64

    try:
        pad = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + pad)
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def decode_sso_meta(sso: str) -> dict:
    """Best-effort parse web SSO JWT payload (not xAI OAuth)."""
    if not sso or sso.count(".") < 2:
        return {}
    return _b64url_json(sso.split(".")[1])


def unique_accounts() -> List[dict]:
    seen = set()
    out = []
    for source, line in collect_all_accounts():
        if line in seen:
            continue
        seen.add(line)
        info = parse_line(line)
        info["source"] = source
        meta = decode_sso_meta(info.get("sso") or "")
        info["session_id"] = meta.get("session_id") or meta.get("sid") or ""
        out.append(info)
    return out


def safe_filename_part(s: str) -> str:
    s = re.sub(r"[^\w.@+-]+", "_", s or "unknown")
    return s[:80] or "unknown"


def account_line_set() -> Set[str]:
    return {line for _, line in collect_all_accounts()}


def load_cpa_index() -> None:
    """Load converted SSO fingerprints + counts from disk."""
    global _cpa_done
    done: Set[str] = set()
    ok_count = 0
    if CPA_INDEX_PATH.exists():
        try:
            data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, dict):
                for fp, meta in items.items():
                    done.add(fp)
                    if isinstance(meta, dict) and meta.get("file"):
                        ok_count += 1
            elif isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("fp"):
                        done.add(it["fp"])
                        ok_count += 1
        except Exception:
            pass
    # also scan existing json files
    for p in CPA_DIR.glob("xai-*.json"):
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            sso = normalize_sso(obj.get("sso") or "")
            if sso:
                done.add(sso_fingerprint(sso))
                ok_count = max(ok_count, 1)
        except Exception:
            continue
    with _cpa_lock:
        _cpa_done = done
        if ok_count and not _cpa_state.get("ok"):
            _cpa_state["ok"] = len(done)


def save_cpa_index_item(fp: str, meta: dict) -> None:
    items: Dict[str, dict] = {}
    if CPA_INDEX_PATH.exists():
        try:
            data = json.loads(CPA_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), dict):
                items = data["items"]
        except Exception:
            items = {}
    items[fp] = meta
    CPA_INDEX_PATH.write_text(
        json.dumps(
            {"updated_at": datetime.now().isoformat(timespec="seconds"), "items": items},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def list_cpa_files() -> List[Path]:
    return sorted(CPA_DIR.glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def cpa_stats() -> dict:
    with _cpa_lock:
        st = dict(_cpa_state)
        done_n = len(_cpa_done)
    files = list_cpa_files()
    st["files"] = len(files)
    st["done"] = done_n
    st["dir"] = str(CPA_DIR)
    return st


def enqueue_cpa_convert(
    email: str,
    sso: str,
    password: str = "",
    source: str = "",
    force: bool = False,
) -> Tuple[bool, str]:
    """Queue one SSO for real OAuth CPA conversion. Returns (queued, reason)."""
    if not AUTO_CPA and not force:
        return False, "auto_cpa disabled"
    if not _CPA_CORE_OK or convert_one is None:
        return False, f"sso2cpa core unavailable: {_CPA_CORE_ERR}"
    sso = normalize_sso(sso)
    if not sso:
        return False, "empty sso"
    fp = sso_fingerprint(sso)
    with _cpa_lock:
        if not force and (fp in _cpa_done or fp in _cpa_inflight):
            return False, "already converted or queued"
        _cpa_inflight.add(fp)
        _cpa_state["pending"] = int(_cpa_state.get("pending") or 0) + 1
    _cpa_q.put(
        {
            "email": email or "",
            "sso": sso,
            "password": password or "",
            "source": source or "",
            "fp": fp,
            "force": force,
        }
    )
    return True, "queued"


def enqueue_new_accounts(before: Set[str]) -> int:
    """Diff account lines after a round and queue new ones."""
    after = account_line_set()
    new_lines = after - before
    n = 0
    for line in new_lines:
        info = parse_line(line)
        ok, _ = enqueue_cpa_convert(
            email=info.get("email") or "",
            sso=info.get("sso") or "",
            password=info.get("password") or "",
            source="register",
        )
        if ok:
            n += 1
    return n


def enqueue_missing_accounts(limit: int = 500) -> int:
    """Queue accounts that have SSO but no CPA file yet."""
    n = 0
    for acc in unique_accounts():
        if n >= limit:
            break
        ok, _ = enqueue_cpa_convert(
            email=acc.get("email") or "",
            sso=acc.get("sso") or "",
            password=acc.get("password") or "",
            source=acc.get("source") or "",
        )
        if ok:
            n += 1
    return n


def _cpa_worker_loop():
    log_line(
        f"[CPA] worker start · core={'ok' if _CPA_CORE_OK else 'FAIL'} · auto={AUTO_CPA} · dir={CPA_DIR}"
    )
    if not _CPA_CORE_OK:
        log_line(f"[CPA] core import error: {_CPA_CORE_ERR}")
    while True:
        item = _cpa_q.get()
        if item is None:
            break
        email = item.get("email") or ""
        sso = item.get("sso") or ""
        fp = item.get("fp") or sso_fingerprint(sso)
        with _cpa_lock:
            _cpa_state["running"] = True
            _cpa_state["pending"] = max(0, int(_cpa_state.get("pending") or 0) - 1)
        try:
            if convert_one is None:
                raise RuntimeError(f"core missing: {_CPA_CORE_ERR}")
            entry = convert_one(sso, email=email, proxy=PROXY_URL)
            # keep password if known (not required by CPA, useful for bookkeeping)
            if item.get("password") and not entry.get("password"):
                entry["password"] = item["password"]
            entry["_source"] = "grok-register-auto-cpa"
            entry["_source_file"] = item.get("source") or ""
            email_out = entry.get("email") or email or "unknown"
            fname = f"xai-{cpa_safe_filename(email_out)}.json"
            path = CPA_DIR / fname
            if path.exists():
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                    old_fp = sso_fingerprint(normalize_sso(old.get("sso") or ""))
                except Exception:
                    old_fp = ""
                if old_fp and old_fp != fp:
                    fname = f"xai-{cpa_safe_filename(email_out)}-{fp[:8]}.json"
                    path = CPA_DIR / fname
            path.write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            save_cpa_index_item(
                fp,
                {
                    "email": email_out,
                    "file": fname,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "auth_kind": entry.get("auth_kind"),
                },
            )
            with _cpa_lock:
                _cpa_done.add(fp)
                _cpa_inflight.discard(fp)
                _cpa_state["ok"] = int(_cpa_state.get("ok") or 0) + 1
                _cpa_state["last_ok_email"] = email_out
                _cpa_state["last_error"] = ""
            log_line(f"[CPA] OK {email_out} -> {fname}")
        except Exception as e:
            err = str(e)
            with _cpa_lock:
                _cpa_inflight.discard(fp)
                _cpa_state["fail"] = int(_cpa_state.get("fail") or 0) + 1
                _cpa_state["last_error"] = err
            try:
                with open(CPA_FAILED_PATH, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "at": datetime.now().isoformat(timespec="seconds"),
                                "email": email,
                                "fp": fp,
                                "error": err,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:
                pass
            log_line(f"[CPA] FAIL {email or fp[:12]}: {err}")
        finally:
            with _cpa_lock:
                _cpa_state["running"] = not _cpa_q.empty()
            if CPA_DELAY > 0:
                time.sleep(CPA_DELAY)
            _cpa_q.task_done()


def start_cpa_worker() -> None:
    load_cpa_index()
    th = threading.Thread(target=_cpa_worker_loop, name="cpa-worker", daemon=True)
    th.start()


def to_grok2api_pool(accounts: List[dict]) -> dict:
    """grok2api-style local token pool using web SSO tokens."""
    tokens = []
    for acc in accounts:
        sso = (acc.get("sso") or "").strip()
        if not sso:
            continue
        tokens.append(
            {
                "token": sso,
                "email": acc.get("email") or "",
                "status": "active",
            }
        )
    return {
        "ssoBasic": tokens,
        "ssoSuper": [],
    }


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def normalize_email_provider(value: str) -> str:
    provider = str(value or "cfworker").strip().lower()
    aliases = {
        "custom": "cfworker",
        "cloudflare": "cloudflare_temp_email",
        "cloudflare-temp-email": "cloudflare_temp_email",
        "yyds": "maliapi",
    }
    if provider in ("tempmailer", "inboxkitten", "inbox_kitten"):
        return "cfworker"
    return aliases.get(provider, provider or "cfworker")


def normalize_cloudflare_temp_email_config(
    data: Optional[dict] = None, fallback: Optional[dict] = None
) -> dict:
    """Return canonical Cloudflare Temp Email fields without conflating cfworker."""
    source = data if isinstance(data, dict) else {}
    old = fallback if isinstance(fallback, dict) else {}

    def pick(key: str, *legacy_keys: str) -> str:
        if key in source:
            return str(source.get(key) or "").strip()
        if key in old:
            return str(old.get(key) or "").strip()
        for legacy in legacy_keys:
            if legacy in source:
                return str(source.get(legacy) or "").strip()
            if legacy in old:
                return str(old.get(legacy) or "").strip()
        return ""

    api_base = pick("cloudflare_api_base").rstrip("/")
    if api_base and not api_base.lower().startswith(("http://", "https://")):
        api_base = f"https://{api_base}"
    return {
        "cloudflare_api_base": api_base,
        "cloudflare_admin_password": pick(
            "cloudflare_admin_password", "cloudflare_api_key"
        ),
        "cloudflare_domain": pick(
            "cloudflare_domain", "defaultDomains"
        ).lower().lstrip("@"),
        "cloudflare_site_password": pick(
            "cloudflare_site_password", "cfworker_custom_auth"
        ),
    }


def validate_cloudflare_temp_email_config(data: dict) -> dict:
    normalized = normalize_cloudflare_temp_email_config(data)
    for key in (
        "cloudflare_api_base",
        "cloudflare_admin_password",
        "cloudflare_domain",
    ):
        if not normalized[key]:
            raise ValueError(f"Cloudflare Temp Email 需要配置: {key}")
    return normalized


def email_config_public(cfg: Optional[dict] = None) -> dict:
    """Email settings for panel UI (multi-provider dropdown)."""
    c = cfg if isinstance(cfg, dict) else load_config()
    provider = normalize_email_provider(c.get("email_provider") or "cfworker")
    cloudflare = normalize_cloudflare_temp_email_config(c)

    choices = [
        {"id": "cfworker", "label": "CF Worker / 自建域名"},
        {
            "id": "cloudflare_temp_email",
            "label": "Cloudflare Temp Email / 自建域名",
        },
        {"id": "moemail", "label": "MoeMail (sall.cc)"},
        {"id": "tempmail_lol", "label": "TempMail.lol（自动生成）"},
        {"id": "duckmail", "label": "DuckMail"},
        {"id": "gptmail", "label": "GPTMail"},
        {"id": "maliapi", "label": "YYDS / MaliAPI"},
        {"id": "luckmail", "label": "LuckMail（接码/买邮）"},
        {"id": "skymail", "label": "SkyMail"},
        {"id": "cloudmail", "label": "CloudMail"},
        {"id": "freemail", "label": "Freemail 自建"},
        {"id": "opentrashmail", "label": "OpenTrashMail"},
        {"id": "laoudo", "label": "Laoudo 固定邮箱"},
    ]
    valid = {x["id"] for x in choices}
    if provider not in valid:
        provider = "cfworker"

    hint = (
        "公共 Tempmailer 已移除（滥用后拒收 xAI 验证码）。"
        "请从下拉框选择邮箱源；自建/CF Worker 通常更稳，公共源可能仍被 xAI 拒绝。"
    )
    return {
        "provider": provider,
        "choices": choices,
        "email_failover": bool(c.get("email_failover", True)),
        # generic CF Worker and dedicated cloudflare_temp_email stay separate
        "cfworker_api_url": str(c.get("cfworker_api_url") or "").strip(),
        "cfworker_admin_token": str(c.get("cfworker_admin_token") or "").strip(),
        "cfworker_domain": str(c.get("cfworker_domain") or "").strip(),
        "cfworker_custom_auth": str(c.get("cfworker_custom_auth") or "").strip(),
        "cfworker_subdomain": str(c.get("cfworker_subdomain") or "").strip(),
        **cloudflare,
        # providers
        "moemail_api_url": str(c.get("moemail_api_url") or "https://sall.cc").strip(),
        "moemail_api_key": str(c.get("moemail_api_key") or "").strip(),
        "gptmail_base_url": str(c.get("gptmail_base_url") or "https://mail.chatgpt.org.uk").strip(),
        "gptmail_api_key": str(c.get("gptmail_api_key") or "").strip(),
        "gptmail_domain": str(c.get("gptmail_domain") or "").strip(),
        "duckmail_api_url": str(c.get("duckmail_api_url") or "https://www.duckmail.sbs").strip(),
        "duckmail_provider_url": str(c.get("duckmail_provider_url") or "https://api.duckmail.sbs").strip(),
        "duckmail_bearer": str(c.get("duckmail_bearer") or "").strip(),
        "duckmail_domain": str(c.get("duckmail_domain") or "").strip(),
        "duckmail_api_key": str(c.get("duckmail_api_key") or "").strip(),
        "maliapi_base_url": str(c.get("maliapi_base_url") or "https://maliapi.215.im/v1").strip(),
        "maliapi_api_key": str(c.get("maliapi_api_key") or c.get("yyds_api_key") or "").strip(),
        "maliapi_domain": str(c.get("maliapi_domain") or "").strip(),
        "luckmail_base_url": str(c.get("luckmail_base_url") or "https://mails.luckyous.com/").strip(),
        "luckmail_api_key": str(c.get("luckmail_api_key") or "").strip(),
        "luckmail_project_code": str(c.get("luckmail_project_code") or "grok").strip(),
        "luckmail_domain": str(c.get("luckmail_domain") or "").strip(),
        "skymail_api_base": str(c.get("skymail_api_base") or "https://api.skymail.ink").strip(),
        "skymail_token": str(c.get("skymail_token") or "").strip(),
        "skymail_domain": str(c.get("skymail_domain") or "").strip(),
        "cloudmail_api_base": str(c.get("cloudmail_api_base") or "").strip(),
        "cloudmail_admin_email": str(c.get("cloudmail_admin_email") or "").strip(),
        "cloudmail_admin_password": str(c.get("cloudmail_admin_password") or "").strip(),
        "cloudmail_domain": str(c.get("cloudmail_domain") or "").strip(),
        "freemail_api_url": str(c.get("freemail_api_url") or "").strip(),
        "freemail_admin_token": str(c.get("freemail_admin_token") or "").strip(),
        "freemail_domain": str(c.get("freemail_domain") or "").strip(),
        "opentrashmail_api_url": str(c.get("opentrashmail_api_url") or "").strip(),
        "opentrashmail_domain": str(c.get("opentrashmail_domain") or "").strip(),
        "opentrashmail_password": str(c.get("opentrashmail_password") or "").strip(),
        "laoudo_auth": str(c.get("laoudo_auth") or "").strip(),
        "laoudo_email": str(c.get("laoudo_email") or "").strip(),
        "laoudo_account_id": str(c.get("laoudo_account_id") or "").strip(),
        "hint": hint,
    }


def apply_email_config_from_ui(data: dict) -> dict:
    """Merge panel email form into config.json and return public view."""
    cfg = load_config()
    raw_provider = str(data.get("provider") or "cfworker").strip().lower()
    if raw_provider in ("tempmailer", "inboxkitten", "inbox_kitten"):
        raise ValueError("内置公共 Tempmailer 已移除，请选择其它邮箱源")
    provider = normalize_email_provider(raw_provider)

    valid = {
        "cfworker", "cloudflare_temp_email", "moemail", "tempmail_lol", "duckmail", "gptmail",
        "maliapi", "luckmail", "skymail", "cloudmail", "freemail", "opentrashmail", "laoudo",
    }
    if provider not in valid:
        raise ValueError(f"不支持的邮箱源: {provider}")

    cfg["email_failover"] = bool(data.get("email_failover", True))
    cfg["email_provider"] = provider
    cfg["email_providers"] = [provider]

    def g(key, default=""):
        return str(data.get(key, cfg.get(key, default)) or default).strip()

    # always store fields (so switching providers keeps values)
    cfg["cfworker_api_url"] = g("cfworker_api_url")
    cfg["cfworker_admin_token"] = g("cfworker_admin_token")
    cfg["cfworker_domain"] = g("cfworker_domain")
    cfg["cfworker_custom_auth"] = g("cfworker_custom_auth")
    cfg["cfworker_subdomain"] = g("cfworker_subdomain")

    cloudflare = normalize_cloudflare_temp_email_config(data, cfg)
    cfg.update(cloudflare)
    # Compatibility reads in older releases. New code never depends on these.
    cfg["cloudflare_api_key"] = cloudflare["cloudflare_admin_password"]
    cfg["defaultDomains"] = cloudflare["cloudflare_domain"]

    for key in (
        "moemail_api_url", "moemail_api_key",
        "gptmail_base_url", "gptmail_api_key", "gptmail_domain",
        "duckmail_api_url", "duckmail_provider_url", "duckmail_bearer", "duckmail_domain", "duckmail_api_key",
        "maliapi_base_url", "maliapi_api_key", "maliapi_domain",
        "luckmail_base_url", "luckmail_api_key", "luckmail_project_code", "luckmail_domain",
        "skymail_api_base", "skymail_token", "skymail_domain",
        "cloudmail_api_base", "cloudmail_admin_email", "cloudmail_admin_password", "cloudmail_domain",
        "freemail_api_url", "freemail_admin_token", "freemail_domain",
        "opentrashmail_api_url", "opentrashmail_domain", "opentrashmail_password",
        "laoudo_auth", "laoudo_email", "laoudo_account_id",
    ):
        if key in data or key in cfg:
            cfg[key] = g(key, cfg.get(key, ""))

    # sync yyds keys for legacy
    if cfg.get("maliapi_api_key") and not cfg.get("yyds_api_key"):
        cfg["yyds_api_key"] = cfg["maliapi_api_key"]

    # required fields soft-check for selected provider
    need = {
        "cfworker": ["cfworker_api_url"],
        "cloudflare_temp_email": [
            "cloudflare_api_base",
            "cloudflare_admin_password",
            "cloudflare_domain",
        ],
        "luckmail": ["luckmail_api_key"],
        "skymail": ["skymail_token"],
        "cloudmail": ["cloudmail_api_base"],
        "freemail": ["freemail_api_url"],
        "opentrashmail": ["opentrashmail_api_url"],
        "laoudo": ["laoudo_email"],
        "maliapi": ["maliapi_api_key"],
    }
    for field in need.get(provider, []):
        if not str(cfg.get(field) or "").strip():
            raise ValueError(f"邮箱源 {provider} 需要配置: {field}")

    cfg.pop("tempmailer_api_base", None)
    cfg.pop("tempmailer_domain", None)
    cfg.pop("tempmailer_domains", None)
    save_config(cfg)
    return email_config_public(cfg)


def probe_cloudflare_temp_email(data: dict) -> dict:
    """Probe settings endpoints only; this must never create an address."""
    import requests

    config = validate_cloudflare_temp_email_config(data)
    headers = {
        "Accept": "application/json",
        "x-admin-auth": config["cloudflare_admin_password"],
        "x-lang": "zh",
    }
    if config["cloudflare_site_password"]:
        headers["x-custom-auth"] = config["cloudflare_site_password"]

    for index, path in enumerate(("/open_api/settings", "/api/settings")):
        try:
            response = requests.request(
                "GET",
                f"{config['cloudflare_api_base']}{path}",
                headers=headers,
                timeout=8,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Cloudflare Temp Email 连接失败: {exc}") from exc
        if 200 <= response.status_code < 300:
            return {
                "ok": True,
                "endpoint": path,
                "message": f"连接成功，设置端点可用: {path}",
            }
        if index == 0 and response.status_code in (404, 405):
            continue
        raise RuntimeError(
            f"Cloudflare Temp Email 连接测试失败: HTTP {response.status_code}"
        )
    raise RuntimeError("Cloudflare Temp Email 设置端点不可用: HTTP 404/405")


def resolve_proxy_url() -> str:
    """Prefer config.json proxy; auto-probe common Clash ports if dead."""
    import socket
    from urllib.parse import urlparse

    def open_port(host: str, port: int, timeout: float = 0.35) -> bool:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    preferred = ""
    try:
        cfg = load_config()
        preferred = str(cfg.get("proxy") or "").strip()
    except Exception:
        preferred = ""
    preferred = preferred or os.environ.get("GROK_PROXY", "").strip() or PROXY_URL

    def ok(url: str) -> bool:
        u = urlparse(url if "://" in url else "http://" + url)
        return open_port(u.hostname or "127.0.0.1", u.port or 7890)

    if preferred and ok(preferred):
        return preferred
    for port in (7897, 7890, 7891, 7892, 10809, 20171, 1080, 2080, 8888):
        url = f"http://127.0.0.1:{port}"
        if ok(url):
            return url
    return preferred or "http://127.0.0.1:7890"


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------- Clash helpers (optional external controller) ---------------
def clash_request(method: str, path: str, data=None, timeout=15):
    if not ENABLE_CLASH_UI:
        raise RuntimeError("clash ui disabled")
    url = CLASH_API + path
    body = None if data is None else json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if CLASH_SECRET:
        headers["Authorization"] = f"Bearer {CLASH_SECRET}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode())


def clash_list_nodes() -> dict:
    """Return usable non-HK leaf nodes + selectors + current."""
    try:
        prox = clash_request("GET", "/proxies")["proxies"]
        cfg = clash_request("GET", "/configs") or {}
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "nodes": [],
            "selectors": {},
            "hint": "未检测到本机 Clash API。请在自己的 Clash 里选节点；本工具默认走 http://127.0.0.1:7890",
        }

    leaves = []
    for name, v in prox.items():
        t = v.get("type") or ""
        if t in (
            "Selector",
            "URLTest",
            "Fallback",
            "LoadBalance",
            "Relay",
            "Direct",
            "Reject",
            "Compatible",
            "Pass",
            "Dns",
        ):
            continue
        if name in ("PASS-RULE", "REJECT-DROP"):
            continue
        if HK_RE.search(name):
            continue
        leaves.append({"name": name, "type": t})

    # sort by region preference
    pref = ["US", "JP", "SG", "TW", "MY", "TH", "UK"]

    def key(n):
        name = n["name"].upper()
        for i, p in enumerate(pref):
            if name.startswith(p):
                return (i, name)
        return (99, name)

    leaves.sort(key=key)

    selectors = {}
    for name, v in prox.items():
        if v.get("type") == "Selector":
            selectors[name] = {"now": v.get("now"), "all": v.get("all") or []}

    return {
        "ok": True,
        "mode": cfg.get("mode"),
        "nodes": leaves,
        "selectors": selectors,
        "global_now": (selectors.get("GLOBAL") or {}).get("now"),
        "main_now": (selectors.get("🚀 使用节点") or {}).get("now"),
    }


def clash_set_node(node: str) -> Tuple[bool, str]:
    if not node:
        return True, "未指定节点（使用外部 Clash 当前节点）"
    if not ENABLE_CLASH_UI:
        return True, "Clash UI 关闭：请在本机 Clash 客户端切换节点"
    try:
        # ensure global mode so browser always uses proxy
        try:
            clash_request("PATCH", "/configs", {"mode": "global"})
        except Exception:
            pass
        prox = clash_request("GET", "/proxies")["proxies"]
        set_count = 0
        for name, v in prox.items():
            if v.get("type") != "Selector":
                continue
            alln = v.get("all") or []
            if node not in alln:
                continue
            try:
                clash_request(
                    "PUT",
                    "/proxies/" + urllib.parse.quote(name, safe=""),
                    {"name": node},
                )
                set_count += 1
            except Exception as e:
                log_line(f"[Clash] set {name} fail: {e}")
        if set_count == 0:
            return False, f"节点 {node} 不在任何选择器中（也可直接在 Clash 客户端切换）"
        return True, f"已切换到 {node}（{set_count} 个选择器）"
    except Exception as e:
        # soft-fail: external Clash without API is OK
        return True, f"Clash API 不可用，跳过切换（{e}）；请在客户端自选节点"


def clash_exit_ip() -> str:
    try:
        proxy_handler = urllib.request.ProxyHandler(
            {"http": PROXY_URL, "https": PROXY_URL}
        )
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(
            "http://ip-api.com/json/?fields=country,city,query,isp", timeout=12
        ) as resp:
            d = json.loads(resp.read().decode())
            return f"{d.get('query')} {d.get('country')}/{d.get('city')} ({d.get('isp')})"
    except Exception as e:
        return f"unknown ({e})"


# --------------- job runner ---------------
def _update_stats_from_log(line: str):
    if "注册成功" in line or "[+] 注册成功" in line:
        with _job_lock:
            _job["success"] = int(_job.get("success") or 0) + 1
    if "注册失败" in line or "[-] 注册失败" in line:
        with _job_lock:
            _job["fail"] = int(_job.get("fail") or 0) + 1


def resolve_round_timeout_sec(cfg: Optional[dict] = None) -> int:
    """Per-account wall-clock timeout (seconds). Default 300; clamp 60..3600."""
    for key in ("ROUND_TIMEOUT_SEC", "ROUND_TIMEOUT"):
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        try:
            return max(60, min(int(float(raw)), 3600))
        except Exception:
            pass
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
        raw_cfg = c.get("round_timeout_sec", DEFAULT_ROUND_TIMEOUT_SEC)
        return max(60, min(int(float(raw_cfg)), 3600))
    except Exception:
        return DEFAULT_ROUND_TIMEOUT_SEC


_ROUND_START_RE = re.compile(
    r"@@GROK_ROUND_START\s+index=(\d+)\s+total=(\d+)\s+attempt=(\d+)"
)
_ROUND_RESULT_RE = re.compile(
    r"@@GROK_ROUND_RESULT\s+index=(\d+)\s+total=(\d+)\s+attempt=(\d+)\s+status=([a-z]+)"
)


def new_batch_marker_state(
    start_index: int,
    batch_count: int,
    total: int,
    round_timeout: int,
    now: Optional[float] = None,
) -> dict:
    current_time = time.time() if now is None else float(now)
    timeout = max(1, int(round_timeout or DEFAULT_ROUND_TIMEOUT_SEC))
    return {
        "start_index": int(start_index),
        "batch_count": max(1, int(batch_count)),
        "total": max(1, int(total)),
        "round_timeout": timeout,
        "current_index": None,
        "deadline": current_time + timeout,
        "seen_results": set(),
        "outcomes": [],
    }


def consume_batch_marker(state: dict, line: str, now: Optional[float] = None):
    current_time = time.time() if now is None else float(now)
    text = str(line or "")
    match = _ROUND_START_RE.search(text)
    if match:
        index, total, attempt = (int(value) for value in match.groups())
        state["current_index"] = index
        state["deadline"] = current_time + int(state["round_timeout"])
        return {
            "kind": "start",
            "index": index,
            "total": total,
            "attempt": attempt,
            "terminal": False,
        }

    match = _ROUND_RESULT_RE.search(text)
    if not match:
        return None
    index, total, attempt = (int(value) for value in match.groups()[:3])
    status = match.group(4).lower()
    terminal = status in ("success", "failed")
    duplicate = terminal and index in state["seen_results"]
    if terminal and not duplicate:
        state["seen_results"].add(index)
        state["outcomes"].append((index, status))
        if state.get("current_index") == index:
            state["current_index"] = None
        cleanup_grace = max(5, min(30, int(state["round_timeout"])))
        state["deadline"] = current_time + cleanup_grace
    return {
        "kind": "result",
        "index": index,
        "total": total,
        "attempt": attempt,
        "status": status,
        "terminal": terminal,
        "duplicate": duplicate,
    }


def remaining_batch_count(total: int, outcomes) -> int:
    completed = {int(index) for index, _ in outcomes}
    return max(0, int(total) - len(completed))


def build_cli_batch_env(
    base_env: dict,
    *,
    batch_count: int,
    round_offset: int,
    total: int,
    engine: str,
    timeout: int,
) -> dict:
    env = dict(base_env or {})
    env["PYTHONUNBUFFERED"] = "1"
    env["GROK_BROWSER_ENGINE"] = str(engine or "chromium")
    env["ROUND_TIMEOUT_SEC"] = str(int(timeout))
    env["GROK_REGISTER_COUNT"] = str(int(batch_count))
    env["GROK_ROUND_OFFSET"] = str(int(round_offset))
    env["GROK_REGISTER_TOTAL"] = str(int(total))
    return env


def supervise_batch_process(
    proc,
    state: dict,
    *,
    stop_requested,
    on_result,
    terminate_proc,
    now=time.time,
    log_callback=None,
) -> dict:
    """Consume CLI markers while enforcing a fresh deadline for every account."""
    line_q: "queue.Queue[Optional[str]]" = queue.Queue()
    reader_done = threading.Event()

    def _stdout_reader() -> None:
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line_q.put(raw)
        except Exception:
            pass
        finally:
            reader_done.set()
            line_q.put(None)

    reader = threading.Thread(
        target=_stdout_reader, name="register-batch-stdout", daemon=True
    )
    reader.start()
    stopped = False
    timed_out = False
    cleanup_timed_out = False
    terminated = False

    def terminate_once() -> None:
        nonlocal terminated
        if not terminated:
            terminate_proc(proc)
            terminated = True

    while True:
        if stop_requested():
            stopped = True
            terminate_once()
            break

        current_time = float(now())
        if current_time >= float(state["deadline"]):
            timed_out = True
            if len(state["outcomes"]) >= state["batch_count"]:
                cleanup_timed_out = True
            else:
                index = state.get("current_index")
                if index is None:
                    index = int(state["start_index"]) + len(state["seen_results"])
                if index not in state["seen_results"]:
                    state["seen_results"].add(index)
                    state["outcomes"].append((index, "failed"))
                    on_result(index, "failed")
            terminate_once()
            break

        try:
            raw = line_q.get(timeout=0.05)
        except queue.Empty:
            if reader_done.is_set() and proc.poll() is not None:
                break
            continue
        if raw is None:
            break

        line = str(raw).rstrip("\r\n")
        event = consume_batch_marker(state, line, now=current_time)
        if event and event.get("kind") == "start":
            with _job_lock:
                _job["current_round"] = event["index"]
                _job["round_deadline"] = state["deadline"]
        if event and event.get("terminal") and not event.get("duplicate"):
            on_result(event["index"], event["status"])
        if log_callback and _is_key_log(line):
            log_callback(_truncate_line(_strip_inner_timestamp(line)))

    if not stopped and not timed_out and len(state["outcomes"]) < state["batch_count"]:
        index = int(state["start_index"]) + len(state["seen_results"])
        if index not in state["seen_results"]:
            state["seen_results"].add(index)
            state["outcomes"].append((index, "failed"))
            on_result(index, "failed")

    return {
        "outcomes": list(state["outcomes"]),
        "stopped": stopped,
        "timed_out": timed_out,
        "cleanup_timed_out": cleanup_timed_out,
        "terminated": terminated,
    }


def _terminate_register_proc(proc: Optional[subprocess.Popen]) -> None:
    """Kill register CLI and its browser children (Windows process tree)."""
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    try:
        if os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
                return
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _run_batch(start_index: int, total: int, batch_count: int) -> dict:
    """Run remaining accounts in one reusable CLI process with per-round deadlines."""
    global _proc
    cfg = load_config()
    batch_count = max(1, int(batch_count))
    # Pass the whole remaining batch through the environment; do not overwrite config.json.
    cfg_run = dict(cfg)
    cfg_run["register_count"] = batch_count
    cfg_run["proxy"] = resolve_proxy_url()
    global PROXY_URL
    PROXY_URL = cfg_run["proxy"]
    os.environ["GROK_PROXY"] = PROXY_URL
    cfg_run.setdefault("email_provider", "cfworker")
    engine = str(cfg_run.get("browser_engine") or "chromium").strip().lower()
    if engine in ("camoufox", "firefox", "headless", "cfox"):
        engine = "camoufox"
    else:
        engine = "chromium"
    cfg_run["browser_engine"] = engine
    # 只把代理/引擎写回；register_count 保持用户原值
    try:
        cfg_save = load_config()
        cfg_save["proxy"] = cfg_run["proxy"]
        cfg_save["browser_engine"] = engine
        if "round_timeout_sec" not in cfg_save:
            cfg_save["round_timeout_sec"] = DEFAULT_ROUND_TIMEOUT_SEC
        save_config(cfg_save)
        cfg = cfg_save
    except Exception:
        cfg = cfg_run

    round_timeout = resolve_round_timeout_sec(cfg)
    env = build_cli_batch_env(
        os.environ.copy(),
        batch_count=batch_count,
        round_offset=start_index - 1,
        total=total,
        engine=engine,
        timeout=round_timeout,
    )
    # Windows / local: use system Chrome/Edge; allow override (chromium engine only)
    if engine == "chromium":
        if os.name == "nt":
            if not env.get("BROWSER_PATH"):
                for cand in (
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                ):
                    if Path(cand).exists():
                        env["BROWSER_PATH"] = cand
                        break
        else:
            env["DISPLAY"] = env.get("DISPLAY") or ":0"
            env.setdefault("BROWSER_PATH", env.get("BROWSER_PATH") or "")

    log_line(
        f"=== 批次开始：第 {start_index}-{start_index + batch_count - 1}/{total} 轮"
        f" · 节点 {_job.get('current_node') or '外部Clash'} ==="
    )
    engine_label = "Camoufox 无头" if engine == "camoufox" else "Chromium 有头"
    log_line(
        f"[*] proxy={PROXY_URL} engine={engine_label} python={VENV_PYTHON} "
        f"round_timeout={round_timeout}s"
    )

    # 注册前检查邮箱源是否可用（公共 Tempmailer 已移除）
    try:
        mail_cfg = load_config()
        mail_prov = normalize_email_provider(
            mail_cfg.get("email_provider") or "cfworker"
        )
        if mail_prov in ("tempmailer", "inboxkitten", "inbox_kitten"):
            log_line("[!] 内置公共临时邮已移除，请在面板下拉选择其它邮箱源")
            return {"outcomes": [], "stopped": False, "timed_out": False, "fatal": True}
        # no-key providers
        free_ok = mail_prov in ("tempmail_lol", "moemail", "gptmail", "duckmail")
        has_cfworker = bool(str(mail_cfg.get("cfworker_api_url") or "").strip())
        cloudflare_cfg = normalize_cloudflare_temp_email_config(mail_cfg)
        has_cloudflare_temp_email = all(
            cloudflare_cfg[key]
            for key in (
                "cloudflare_api_base",
                "cloudflare_admin_password",
                "cloudflare_domain",
            )
        )
        has_luck = bool(str(mail_cfg.get("luckmail_api_key") or "").strip())
        has_mali = bool(str(mail_cfg.get("maliapi_api_key") or mail_cfg.get("yyds_api_key") or "").strip())
        has_sky = bool(str(mail_cfg.get("skymail_token") or "").strip())
        has_cloud = bool(str(mail_cfg.get("cloudmail_api_base") or "").strip())
        has_free = bool(str(mail_cfg.get("freemail_api_url") or "").strip())
        has_otm = bool(str(mail_cfg.get("opentrashmail_api_url") or "").strip())
        has_lao = bool(str(mail_cfg.get("laoudo_email") or "").strip())
        ok = free_ok
        if mail_prov == "cfworker":
            ok = has_cfworker
        elif mail_prov == "cloudflare_temp_email":
            ok = has_cloudflare_temp_email
        elif mail_prov == "luckmail":
            ok = has_luck
        elif mail_prov in ("maliapi", "yyds"):
            ok = has_mali
        elif mail_prov == "skymail":
            ok = has_sky
        elif mail_prov == "cloudmail":
            ok = has_cloud
        elif mail_prov == "freemail":
            ok = has_free
        elif mail_prov == "opentrashmail":
            ok = has_otm
        elif mail_prov == "laoudo":
            ok = has_lao
        if not ok:
            log_line(f"[!] 邮箱源 {mail_prov} 尚未配置完整，请到面板「邮箱服务」填写后保存")
            return {
                "outcomes": [],
                "stopped": False,
                "timed_out": False,
                "fatal": True,
            }
        log_line(f"[*] 邮箱源: {mail_prov}")
    except Exception as e:
        log_line(f"[!] 检查邮箱配置失败: {e}")
        return {"outcomes": [], "stopped": False, "timed_out": False, "fatal": True}

    # Camoufox 首次要下载浏览器二进制，不计入 5 分钟注册超时
    if engine == "camoufox":
        try:
            lib_dir = str(BASE_DIR / "lib")
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            from camoufox_backend import ensure_camoufox_ready  # type: ignore

            log_line("[*] 检查 Camoufox 浏览器（首次会下载，可能几分钟）...")
            exe = ensure_camoufox_ready(log_callback=log_line)
            log_line(f"[*] Camoufox 就绪: {exe}")
        except Exception as e:
            log_line(f"[!] Camoufox 准备失败: {e}")
            log_line("[!] 可改用 Chromium 有头引擎，或手动执行: .venv\\Scripts\\python.exe -m camoufox fetch")
            return {"outcomes": [], "stopped": False, "timed_out": False, "fatal": True}

    cmd = [
        VENV_PYTHON,
        "-u",
        str(MAIN_SCRIPT),
        "cli",
    ]
    try:
        _proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as e:
        log_line(f"[!] 启动失败: {e}")
        return {"outcomes": [], "stopped": False, "timed_out": False, "fatal": True}

    with _job_lock:
        _job["pid"] = _proc.pid
        _job["round_timeout_sec"] = round_timeout
        _job["round_deadline"] = time.time() + round_timeout

    # send start
    try:
        assert _proc.stdin is not None
        _proc.stdin.write("start\n")
        _proc.stdin.flush()
    except Exception as e:
        log_line(f"[!] 写入 start 失败: {e}")

    known_lines = account_line_set()

    def on_result(index: int, status: str):
        nonlocal known_lines
        after_lines = account_line_set()
        new_line_count = len(after_lines - known_lines)
        queued = 0
        if AUTO_CPA and new_line_count:
            queued = enqueue_new_accounts(known_lines)
        known_lines = after_lines
        adjusted = status
        with _job_lock:
            _job["current_round"] = index
            key = "success" if adjusted == "success" else "fail"
            _job[key] = int(_job.get(key) or 0) + 1
        if queued:
            log_line(f"[CPA] 第 {index} 轮新账号入队转换: {queued}")
        if adjusted == "success":
            log_line(f"[+] 第 {index} 轮成功（累计成功 {_job['success']}）")
        else:
            if new_line_count:
                log_line(
                    f"[!] 第 {index} 轮标记为失败，但发现 {new_line_count} 条新账号记录；"
                    "统计仍以 ROUND_RESULT 为准"
                )
            log_line(f"[-] 第 {index} 轮失败（累计失败 {_job['fail']}）")
        return adjusted

    state = new_batch_marker_state(
        start_index=start_index,
        batch_count=batch_count,
        total=total,
        round_timeout=round_timeout,
    )
    summary = supervise_batch_process(
        _proc,
        state,
        stop_requested=lambda: bool(_job.get("stop")),
        on_result=on_result,
        terminate_proc=_terminate_register_proc,
        log_callback=log_line,
    )
    if summary["stopped"]:
        log_line("[!] 收到停止指令，已终止当前批次")
    if summary.get("cleanup_timed_out"):
        log_line("[!] 全部轮次已完成，但 CLI 清理超时；已终止所拥有的进程树")
    elif summary["timed_out"]:
        timed_out_index = state.get("current_index") or (
            start_index + len(state["outcomes"]) - 1
        )
        log_line(
            f"[!] 第 {timed_out_index} 轮超时（{round_timeout}s），"
            "已终止所拥有的进程树，剩余账号将由新批次继续"
        )
        with _job_lock:
            _job["last_error"] = (
                f"round {timed_out_index} timeout after {round_timeout}s"
            )

    if _proc is not None and _proc.poll() is None and not summary["terminated"]:
        _terminate_register_proc(_proc)
    try:
        if _proc is not None:
            _proc.wait(timeout=15)
    except Exception:
        _terminate_register_proc(_proc)

    with _job_lock:
        _job["pid"] = None
        _job.pop("round_deadline", None)
    _proc = None
    summary["fatal"] = False
    return summary


def _next_node(nodes: List[str], index: int) -> Tuple[str, int]:
    if not nodes:
        return "", 0
    index = (index + 1) % len(nodes)
    return nodes[index], index


def job_worker(count: int, node: str = "", node_mode: str = "fixed", node_list: Optional[List[str]] = None):
    """Run register rounds. Node switching is intentionally not managed here —
    user selects nodes in their own Clash client."""
    global _job
    try:
        with _job_lock:
            _job["running"] = True
            _job["stop"] = False
            _job["status"] = "running"
            _job["count"] = count
            _job["success"] = 0
            _job["fail"] = 0
            _job["current_round"] = 0
            _job["node_mode"] = "external"
            _job["node_list"] = []
            _job["current_node"] = "external-clash"
            _job["started_at"] = datetime.now().isoformat(timespec="seconds")
            _job["finished_at"] = None
            _job["last_error"] = ""

        proxy_now = resolve_proxy_url()
        global PROXY_URL
        PROXY_URL = proxy_now
        os.environ["GROK_PROXY"] = proxy_now
        try:
            cfg0 = load_config(); cfg0["proxy"] = proxy_now; save_config(cfg0)
        except Exception:
            pass
        log_line(f"[*] 使用外部 Clash 代理: {proxy_now}（节点请在 Clash 客户端选择）")
        log_line(f"[*] 出口探测: {clash_exit_ip()}")

        completed = 0
        while completed < count:
            if _job.get("stop"):
                log_line("[!] 用户停止，结束任务")
                break
            remaining = count - completed
            summary = _run_batch(
                start_index=completed + 1,
                total=count,
                batch_count=remaining,
            )
            finished_now = len(summary.get("outcomes") or [])
            completed += finished_now
            if summary.get("stopped") or _job.get("stop"):
                break
            if summary.get("fatal"):
                log_line("[!] 批次无法启动，结束任务")
                break
            if finished_now <= 0:
                log_line("[!] 批次未返回任何轮次结果，为避免空转而结束")
                break
            if completed < count:
                log_line(
                    f"[*] 重新拉起剩余批次：已完成 {completed}，剩余 {count - completed}"
                )

        log_line(
            f"[*] 全部结束：成功 {_job.get('success')} | 失败 {_job.get('fail')} / 目标 {count}"
        )
    except Exception as e:
        log_line(f"[!] 任务异常: {e}")
        log_line(traceback.format_exc())
        with _job_lock:
            _job["last_error"] = str(e)
    finally:
        with _job_lock:
            _job["running"] = False
            _job["status"] = "idle"
            _job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _job["pid"] = None


def start_job(count: int, node: str = "", node_mode: str = "fixed") -> Tuple[bool, str]:
    with _job_lock:
        if _job.get("running"):
            return False, "已有任务在运行"
    if count < 1 or count > 500:
        return False, "轮数范围 1-500"

    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    with _job_lock:
        _job["log_path"] = str(log_path)
    _logs.clear()
    log_line(f"任务创建：轮数={count} proxy={PROXY_URL}（节点由本机 Clash 管理）")

    th = threading.Thread(
        target=job_worker,
        args=(count,),
        daemon=True,
    )
    th.start()
    return True, "已启动"


def stop_job() -> Tuple[bool, str]:
    with _job_lock:
        if not _job.get("running"):
            return False, "当前没有运行中的任务"
        _job["stop"] = True
    log_line("[!] 正在停止…")
    return True, "已发送停止"


# --------------- HTML ---------------
LOGIN_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>登录 · Grok Register</title>
  <style>
    :root{--bg:#0b0e14;--card:#141a26;--fg:#eef2fb;--muted:#8b97b0;--line:#222b3d;--accent:#6ea8fe;--accent2:#4f8cff}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
      background:radial-gradient(1200px 600px at 20% -10%,#1a2540 0%,transparent 55%),radial-gradient(900px 500px at 80% 100%,#1a1f3a 0%,transparent 50%),var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
    .card{width:min(420px,92vw);background:var(--card);border:1px solid var(--line);border-radius:18px;padding:32px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .brand{display:flex;align-items:center;gap:12px;margin-bottom:6px}
    .logo{width:40px;height:40px;border-radius:11px;background:#000;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:900;color:#fff;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.35);letter-spacing:-1px}
    h1{margin:0;font-size:22px;font-weight:700} p{margin:6px 0 22px;color:var(--muted);font-size:13.5px}
    input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0f131c;color:var(--fg);font-size:14px;font-family:inherit;transition:border-color .15s}
    input:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.15)}
    button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;font-weight:600;font-size:14px;cursor:pointer;transition:box-shadow .15s}
    button:hover{box-shadow:0 6px 18px rgba(79,140,255,.45)}
    .err{color:#ff8f8f;margin-top:10px;font-size:13px}
  </style>
</head>
<body>
<form class="card" method="post">
  <div class="brand"><div class="logo">G</div><h1>Grok Register</h1></div>
  <p>账号面板 · 启动注册 · 外置 Clash 代理</p>
  <input type="password" name="password" placeholder="面板密码" autofocus required/>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <button type="submit">进入</button>
</form>
</body></html>
"""

INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Grok Register 面板</title>
  <style>
    :root{
      --bg:#0b0e14;--bg2:#0f131c;--card:#141a26;--card2:#1a2130;--fg:#eef2fb;--muted:#8b97b0;--muted2:#6b7793;
      --accent:#6ea8fe;--accent2:#4f8cff;--ok:#3dd68c;--bad:#ff7b7b;--warn:#ffb454;
      --line:#222b3d;--line2:#2c3650;--chip:#1c2434;--chip2:#222c40;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:
      radial-gradient(1200px 600px at 12% -18%,#1a2540 0%,transparent 55%),
      radial-gradient(900px 500px at 92% 8%,#1a1f3a 0%,transparent 50%),
      var(--bg);color:var(--fg);min-height:100vh;-webkit-font-smoothing:antialiased}
    .wrap{max-width:1200px;margin:0 auto;padding:24px 16px 56px}
    header{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:18px;border-bottom:1px solid var(--line)}
    .brand{display:flex;align-items:center;gap:14px}
    .logo{width:42px;height:42px;border-radius:12px;background:#000;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900;color:#fff;flex-shrink:0;box-shadow:0 6px 18px rgba(0,0,0,.35);letter-spacing:-1px}
    h1{margin:0;font-size:22px;font-weight:700;letter-spacing:.3px} .sub{color:var(--muted);font-size:12.5px;margin-top:3px}
    .actions{display:flex;flex-wrap:wrap;gap:10px}
    a.btn,button.btn{border:1px solid var(--line2);background:var(--chip);color:var(--fg);padding:10px 14px;border-radius:10px;text-decoration:none;font-size:13px;cursor:pointer;transition:all .15s ease;display:inline-flex;align-items:center;gap:6px}
    a.btn:hover,button.btn:hover{background:var(--chip2);border-color:var(--accent);transform:translateY(-1px)}
    a.btn:active,button.btn:active{transform:translateY(0)}
    a.btn.primary,button.btn.primary{background:linear-gradient(135deg,var(--accent2),var(--accent));border-color:transparent;color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(79,140,255,.3)}
    a.btn.primary:hover,button.btn.primary:hover{box-shadow:0 6px 18px rgba(79,140,255,.45)}
    a.btn.ok,button.btn.ok{background:linear-gradient(135deg,#1f9d63,#3dd68c);border:0;color:#042;font-weight:600;box-shadow:0 4px 12px rgba(61,214,140,.25)}
    a.btn.ok:hover,button.btn.ok:hover{box-shadow:0 6px 18px rgba(61,214,140,.4)}
    a.btn.sub2,button.btn.sub2{background:linear-gradient(135deg,#6d28d9,#a78bfa);border:0;color:#fff;font-weight:600;box-shadow:0 4px 12px rgba(167,139,250,.28)}
    a.btn.sub2:hover,button.btn.sub2:hover{box-shadow:0 6px 18px rgba(167,139,250,.45)}
    a.btn.danger,button.btn.danger{background:#2a1717;border-color:#5a2b2b;color:#ffb4b4}
    a.btn.danger:hover,button.btn.danger:hover{background:#381c1c}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0 20px}
    .stat{background:linear-gradient(180deg,var(--card) 0%,var(--card2) 100%);border:1px solid var(--line);border-radius:14px;padding:14px 16px;position:relative;overflow:hidden;transition:border-color .15s}
    .stat:hover{border-color:var(--accent)}
    .stat::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),transparent);opacity:.7}
    .stat .k{color:var(--muted2);font-size:11.5px;text-transform:uppercase;letter-spacing:.5px}
    .stat .v{font-size:22px;font-weight:700;margin-top:6px;color:var(--fg)}
    .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px;box-shadow:0 4px 16px rgba(0,0,0,.15)}
    .card h2{margin:0 0 14px;font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
    .card h2::before{content:"";width:3px;height:14px;background:linear-gradient(180deg,var(--accent),var(--accent2));border-radius:2px}
    .row{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
    label{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted)}
    input,select{background:var(--bg2);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:10px 12px;min-width:150px;font-size:13px;transition:border-color .15s;font-family:inherit}
    input:focus,select:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(110,168,254,.15)}
    table{width:100%;border-collapse:collapse}
    th,td{padding:11px 14px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
    th{color:var(--muted);background:var(--bg2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
    tbody tr{transition:background .12s}
    tbody tr:hover{background:rgba(110,168,254,.04)}
    .mono{font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;word-break:break-all;font-size:12.5px}
    .muted{color:var(--muted)} .tag{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--chip);color:var(--accent);font-size:12px;font-weight:500}
    #logbox{height:340px;overflow:auto;background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:14px;font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;font-size:12.5px;line-height:1.5;white-space:pre-wrap;color:var(--muted)}
    #logbox::-webkit-scrollbar{width:8px}
    #logbox::-webkit-scrollbar-thumb{background:var(--line2);border-radius:4px}
    .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;background:#555;vertical-align:middle}
    .dot.run{background:var(--ok);box-shadow:0 0 10px var(--ok);animation:pulse 1.5s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
    .toast{position:fixed;right:20px;bottom:20px;background:var(--card2);border:1px solid var(--line2);padding:12px 16px;border-radius:10px;display:none;z-index:9;box-shadow:0 8px 24px rgba(0,0,0,.4);font-size:13px}
    code{background:var(--chip);padding:2px 6px;border-radius:4px;font-size:12px;color:var(--accent)}
    @media(max-width:800px){ th:nth-child(3),td:nth-child(3){display:none} .row{flex-direction:column;align-items:stretch} input,select{min-width:0} }
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">G</div>
      <div>
        <h1>Grok Register</h1>
        <div class="sub">{{ base_dir }} · 代理走本机 Clash（Clash Verge 默认 7897）</div>
      </div>
    </div>
    <div class="actions">
      <a class="btn primary" href="/download/sso.txt" title="email----password----sso">⬇ 下载 SSO (TXT)</a>
      <a class="btn ok" href="/download/cpa.zip" title="CPA OAuth JSON（CLIProxyAPI 可用）">⬇ 下载 CPA (JSON)</a>
      <a class="btn sub2" href="/download/sub2.zip" title="Sub2API 官方导入包 type=sub2api-data：单账号 JSON + all 合集">⬇ 下载 Sub2 (JSON)</a>
    </div>
  </header>

  <div class="grid">
    <div class="stat"><div class="k">文件数</div><div class="v" id="st_files">{{ file_count }}</div></div>
    <div class="stat"><div class="k">SSO 账号</div><div class="v" id="st_accounts">{{ account_count }}</div></div>
    <div class="stat"><div class="k">CPA 已转换</div><div class="v" id="st_cpa_ok">{{ cpa_files }}</div></div>
    <div class="stat"><div class="k">CPA 队列</div><div class="v" style="font-size:16px" id="st_cpa_q">0 / 0 / 0</div></div>
    <div class="stat"><div class="k">任务状态</div><div class="v" style="font-size:16px"><span class="dot" id="st_dot"></span><span id="st_status">idle</span></div></div>
    <div class="stat"><div class="k">注册 成功/失败</div><div class="v" style="font-size:16px"><span id="st_sf">0 / 0</span></div></div>
  </div>

  <div class="card">
    <h2>启动注册</h2>
    <div class="row">
      <label>轮数
        <input type="number" id="count" min="1" max="500" value="1"/>
      </label>
      <label>浏览器引擎
        <select id="browser_engine" onchange="saveBrowserEngine()">
          <option value="chromium">Chromium 有头（默认）</option>
          <option value="camoufox">Camoufox 无头（反检测 Firefox）</option>
        </select>
      </label>
      <button class="btn ok" id="btn_start" onclick="startJob()">▶ 开始注册</button>
      <button class="btn danger" id="btn_stop" onclick="stopJob()">■ 停止</button>
      <button class="btn" onclick="backfillCpa()" title="把尚未转成 CPA 的历史 SSO 入队">补转未转换 CPA</button>
    </div>
    <div class="muted" style="margin-top:10px;font-size:12px" id="cpa_hint">
      代理走本机 Clash（config.json 的 proxy，常见 7897）。节点在 Clash 里选。注册成功后自动转 CPA。
      Camoufox 首次使用会自动下载浏览器二进制。
    </div>
    <div class="muted" style="margin-top:8px;font-size:12px;line-height:1.55">
      提示：绝大多数注册失败来自网络环境，而非脚本本身。实测机场节点里<strong style="color:var(--ok);font-weight:600">日本</strong>更稳；
      新加坡 / 美国 / 德国成功率偏低。失败时请先在 Clash 换日本节点再试。
    </div>
  </div>

  <div class="card">
    <h2>邮箱服务</h2>
    <div class="muted" style="font-size:12px;margin:0 0 10px;line-height:1.55;padding:10px 12px;border:1px solid #5b3b14;background:rgba(180,100,20,.12);border-radius:10px;color:#f0c674">
      公共 Tempmailer 已移除（滥用后拒收 xAI 验证码）。请用下拉框选择邮箱源；自建/CF Worker 通常更稳，公共源可能仍被拒。
    </div>
    <div class="row">
      <label>邮箱源
        <select id="email_provider" onchange="onEmailProviderChange()"></select>
      </label>
      <label style="min-width:auto;flex-direction:row;align-items:center;gap:8px;padding-bottom:10px">
        <input type="checkbox" id="email_failover" style="width:auto;min-width:0"/> 失败时自动换源
      </label>
      <button class="btn primary" onclick="saveEmailConfig()">保存邮箱设置</button>
      <button class="btn" id="btn_email_test" onclick="testCloudflareEmailConnection()" style="display:none">测试连接</button>
    </div>

    <div id="box_cfworker" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="cfworker_api_url" placeholder="https://apimail.example.com"/>
        </label>
        <label>Admin Token
          <input type="password" id="cfworker_admin_token" placeholder="管理员密钥"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>域名
          <input type="text" id="cfworker_domain" placeholder="mail.example.com"/>
        </label>
        <label>站点密码
          <input type="password" id="cfworker_custom_auth" placeholder="可选"/>
        </label>
        <label>子域名
          <input type="text" id="cfworker_subdomain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_cloudflare_temp_email" class="mail-box" style="display:none;margin-top:10px">
      <div class="muted" style="font-size:12px;margin-bottom:8px;line-height:1.55">
        对接 dreamhunter2333/cloudflare_temp_email：管理员接口创建地址，地址 JWT 拉取解析邮件。连接测试只读取设置，不会创建邮箱。
      </div>
      <div class="row">
        <label style="flex:2">API 根地址
          <input type="url" id="cloudflare_api_base" placeholder="https://mail.example.com" autocomplete="url"/>
        </label>
        <label>管理员密码
          <input type="password" id="cloudflare_admin_password" placeholder="x-admin-auth" autocomplete="new-password"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>域名
          <input type="text" id="cloudflare_domain" placeholder="mail.example.com" autocomplete="off"/>
        </label>
        <label>站点访问密码
          <input type="password" id="cloudflare_site_password" placeholder="可选，x-custom-auth" autocomplete="new-password"/>
        </label>
      </div>
    </div>

    <div id="box_moemail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="moemail_api_url" placeholder="https://sall.cc"/>
        </label>
        <label>API Key
          <input type="password" id="moemail_api_key" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_tempmail_lol" class="mail-box" style="display:none;margin-top:10px">
      <div class="muted" style="font-size:12px">TempMail.lol：无需 Key，自动生成邮箱后轮询收信（可能被 xAI 拒绝）。</div>
    </div>

    <div id="box_duckmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label>Web URL
          <input type="text" id="duckmail_api_url" placeholder="https://www.duckmail.sbs"/>
        </label>
        <label>Provider URL
          <input type="text" id="duckmail_provider_url" placeholder="https://api.duckmail.sbs"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>Bearer
          <input type="password" id="duckmail_bearer" placeholder="可选"/>
        </label>
        <label>API Key
          <input type="password" id="duckmail_api_key" placeholder="可选"/>
        </label>
        <label>域名
          <input type="text" id="duckmail_domain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_gptmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="gptmail_base_url" placeholder="https://mail.chatgpt.org.uk"/>
        </label>
        <label>API Key
          <input type="password" id="gptmail_api_key" placeholder="可选"/>
        </label>
        <label>域名
          <input type="text" id="gptmail_domain" placeholder="可选，填了则本地拼地址"/>
        </label>
      </div>
    </div>

    <div id="box_maliapi" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="maliapi_base_url" placeholder="https://maliapi.215.im/v1"/>
        </label>
        <label>API Key
          <input type="password" id="maliapi_api_key"/>
        </label>
        <label>域名
          <input type="text" id="maliapi_domain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_luckmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">平台地址
          <input type="text" id="luckmail_base_url" placeholder="https://mails.luckyous.com"/>
        </label>
        <label>API Key
          <input type="password" id="luckmail_api_key"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>项目代码
          <input type="text" id="luckmail_project_code" placeholder="grok"/>
        </label>
        <label>域名
          <input type="text" id="luckmail_domain" placeholder="可选"/>
        </label>
      </div>
    </div>

    <div id="box_skymail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API Base
          <input type="text" id="skymail_api_base" placeholder="https://api.skymail.ink"/>
        </label>
        <label>Token
          <input type="password" id="skymail_token"/>
        </label>
        <label>域名
          <input type="text" id="skymail_domain"/>
        </label>
      </div>
    </div>

    <div id="box_cloudmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API Base
          <input type="text" id="cloudmail_api_base"/>
        </label>
        <label>管理员邮箱
          <input type="text" id="cloudmail_admin_email"/>
        </label>
        <label>管理员密码
          <input type="password" id="cloudmail_admin_password"/>
        </label>
      </div>
      <div class="row" style="margin-top:8px">
        <label>域名
          <input type="text" id="cloudmail_domain"/>
        </label>
      </div>
    </div>

    <div id="box_freemail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="freemail_api_url"/>
        </label>
        <label>Admin Token
          <input type="password" id="freemail_admin_token"/>
        </label>
        <label>域名
          <input type="text" id="freemail_domain"/>
        </label>
      </div>
    </div>

    <div id="box_opentrashmail" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label style="flex:2">API URL
          <input type="text" id="opentrashmail_api_url"/>
        </label>
        <label>域名
          <input type="text" id="opentrashmail_domain"/>
        </label>
        <label>密码
          <input type="password" id="opentrashmail_password"/>
        </label>
      </div>
    </div>

    <div id="box_laoudo" class="mail-box" style="display:none;margin-top:10px">
      <div class="row">
        <label>Auth
          <input type="password" id="laoudo_auth"/>
        </label>
        <label>邮箱
          <input type="text" id="laoudo_email"/>
        </label>
        <label>Account ID
          <input type="text" id="laoudo_account_id"/>
        </label>
      </div>
    </div>

    <div class="muted" style="margin-top:10px;font-size:12px;display:none" id="email_hint"></div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div id="logbox">等待任务…</div>
  </div>

  <div class="card" style="padding:0;overflow:hidden">
    <div style="padding:14px 14px 0;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between">
      <h2 style="margin:0">账号文件</h2>
      <div class="actions" style="margin:0">
        <button class="btn" type="button" onclick="toggleSelectAllFiles(true)">全选</button>
        <button class="btn" type="button" onclick="toggleSelectAllFiles(false)">取消全选</button>
        <button class="btn danger" type="button" onclick="deleteSelectedFiles()">删除选中</button>
      </div>
    </div>
    <div class="muted" style="padding:8px 14px 0;font-size:12px">勾选已下载/不需要的 accounts_*.txt，删除后不会再出现在「下载 SSO」合并结果里。</div>
    {% if files %}
    <table>
      <thead>
        <tr>
          <th style="width:44px"><input type="checkbox" id="chk_all_files" onclick="toggleSelectAllFiles(this.checked)" title="全选"/></th>
          <th>文件</th><th>数量</th><th>时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
      {% for f in files %}
        <tr>
          <td><input type="checkbox" class="chk-file" value="{{ f.name }}"/></td>
          <td class="mono">{{ f.name }}</td>
          <td><span class="tag">{{ f.count }}</span></td>
          <td class="muted">{{ f.mtime }}</td>
          <td>
            <a class="btn" href="/preview/{{ f.name }}">预览</a>
            <a class="btn primary" href="/download/{{ f.name }}">下载</a>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="padding:24px;color:var(--muted);text-align:center">暂无 accounts_*.txt</div>
    {% endif %}
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2200)}
async function api(url, opt){
  const r = await fetch(url, Object.assign({credentials:'same-origin'}, opt||{}));
  const j = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.statusText||'request failed');
  return j;
}
function onEmailProviderChange(){
  const p=document.getElementById('email_provider').value||'cfworker';
  document.querySelectorAll('.mail-box').forEach(el=>{ el.style.display='none'; });
  const box=document.getElementById('box_'+p);
  if(box) box.style.display='block';
  const testButton=document.getElementById('btn_email_test');
  if(testButton) testButton.style.display=p==='cloudflare_temp_email'?'':'none';
}
function _val(id){const el=document.getElementById(id); return el?el.value:'';}
function _set(id,v){const el=document.getElementById(id); if(el) el.value=v||'';}
function _check(id,v){const el=document.getElementById(id); if(el) el.checked=!!v;}
async function loadEmailConfig(){
  try{
    const j=await api('/api/config/email');
    const e=j.email||{};
    const sel=document.getElementById('email_provider');
    sel.innerHTML='';
    (e.choices||[]).forEach(c=>{
      const o=document.createElement('option');
      o.value=c.id; o.textContent=c.label;
      sel.appendChild(o);
    });
    let prov=e.provider||'cfworker';
    if(![...sel.options].some(o=>o.value===prov)) prov='cfworker';
    sel.value=prov;
    _check('email_failover', e.email_failover);
    _set('cfworker_api_url', e.cfworker_api_url);
    _set('cfworker_admin_token', e.cfworker_admin_token);
    _set('cfworker_domain', e.cfworker_domain);
    _set('cfworker_custom_auth', e.cfworker_custom_auth);
    _set('cfworker_subdomain', e.cfworker_subdomain);
    _set('cloudflare_api_base', e.cloudflare_api_base);
    _set('cloudflare_admin_password', e.cloudflare_admin_password);
    _set('cloudflare_domain', e.cloudflare_domain);
    _set('cloudflare_site_password', e.cloudflare_site_password);
    _set('moemail_api_url', e.moemail_api_url||'https://sall.cc');
    _set('moemail_api_key', e.moemail_api_key);
    _set('gptmail_base_url', e.gptmail_base_url||'https://mail.chatgpt.org.uk');
    _set('gptmail_api_key', e.gptmail_api_key);
    _set('gptmail_domain', e.gptmail_domain);
    _set('duckmail_api_url', e.duckmail_api_url||'https://www.duckmail.sbs');
    _set('duckmail_provider_url', e.duckmail_provider_url||'https://api.duckmail.sbs');
    _set('duckmail_bearer', e.duckmail_bearer);
    _set('duckmail_api_key', e.duckmail_api_key);
    _set('duckmail_domain', e.duckmail_domain);
    _set('maliapi_base_url', e.maliapi_base_url||'https://maliapi.215.im/v1');
    _set('maliapi_api_key', e.maliapi_api_key);
    _set('maliapi_domain', e.maliapi_domain);
    _set('luckmail_base_url', e.luckmail_base_url||'https://mails.luckyous.com');
    _set('luckmail_api_key', e.luckmail_api_key);
    _set('luckmail_project_code', e.luckmail_project_code||'grok');
    _set('luckmail_domain', e.luckmail_domain);
    _set('skymail_api_base', e.skymail_api_base||'https://api.skymail.ink');
    _set('skymail_token', e.skymail_token);
    _set('skymail_domain', e.skymail_domain);
    _set('cloudmail_api_base', e.cloudmail_api_base);
    _set('cloudmail_admin_email', e.cloudmail_admin_email);
    _set('cloudmail_admin_password', e.cloudmail_admin_password);
    _set('cloudmail_domain', e.cloudmail_domain);
    _set('freemail_api_url', e.freemail_api_url);
    _set('freemail_admin_token', e.freemail_admin_token);
    _set('freemail_domain', e.freemail_domain);
    _set('opentrashmail_api_url', e.opentrashmail_api_url);
    _set('opentrashmail_domain', e.opentrashmail_domain);
    _set('opentrashmail_password', e.opentrashmail_password);
    _set('laoudo_auth', e.laoudo_auth);
    _set('laoudo_email', e.laoudo_email);
    _set('laoudo_account_id', e.laoudo_account_id);
    setEmailHint(e.hint||'');
    onEmailProviderChange();
  }catch(err){
    setEmailHint('加载邮箱配置失败: '+err.message);
  }
}
async function saveEmailConfig(){
  const body={
    provider: (document.getElementById('email_provider').value||'cfworker'),
    email_failover: document.getElementById('email_failover').checked,
    cfworker_api_url: _val('cfworker_api_url'),
    cfworker_admin_token: _val('cfworker_admin_token'),
    cfworker_domain: _val('cfworker_domain'),
    cfworker_custom_auth: _val('cfworker_custom_auth'),
    cfworker_subdomain: _val('cfworker_subdomain'),
    cloudflare_api_base: _val('cloudflare_api_base'),
    cloudflare_admin_password: _val('cloudflare_admin_password'),
    cloudflare_domain: _val('cloudflare_domain'),
    cloudflare_site_password: _val('cloudflare_site_password'),
    moemail_api_url: _val('moemail_api_url'),
    moemail_api_key: _val('moemail_api_key'),
    gptmail_base_url: _val('gptmail_base_url'),
    gptmail_api_key: _val('gptmail_api_key'),
    gptmail_domain: _val('gptmail_domain'),
    duckmail_api_url: _val('duckmail_api_url'),
    duckmail_provider_url: _val('duckmail_provider_url'),
    duckmail_bearer: _val('duckmail_bearer'),
    duckmail_api_key: _val('duckmail_api_key'),
    duckmail_domain: _val('duckmail_domain'),
    maliapi_base_url: _val('maliapi_base_url'),
    maliapi_api_key: _val('maliapi_api_key'),
    maliapi_domain: _val('maliapi_domain'),
    luckmail_base_url: _val('luckmail_base_url'),
    luckmail_api_key: _val('luckmail_api_key'),
    luckmail_project_code: _val('luckmail_project_code'),
    luckmail_domain: _val('luckmail_domain'),
    skymail_api_base: _val('skymail_api_base'),
    skymail_token: _val('skymail_token'),
    skymail_domain: _val('skymail_domain'),
    cloudmail_api_base: _val('cloudmail_api_base'),
    cloudmail_admin_email: _val('cloudmail_admin_email'),
    cloudmail_admin_password: _val('cloudmail_admin_password'),
    cloudmail_domain: _val('cloudmail_domain'),
    freemail_api_url: _val('freemail_api_url'),
    freemail_admin_token: _val('freemail_admin_token'),
    freemail_domain: _val('freemail_domain'),
    opentrashmail_api_url: _val('opentrashmail_api_url'),
    opentrashmail_domain: _val('opentrashmail_domain'),
    opentrashmail_password: _val('opentrashmail_password'),
    laoudo_auth: _val('laoudo_auth'),
    laoudo_email: _val('laoudo_email'),
    laoudo_account_id: _val('laoudo_account_id'),
  };
  try{
    const j=await api('/api/config/email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(j.message||'邮箱设置已保存');
    if(j.email){
      setEmailHint('已保存 · 当前: '+(j.email.provider||''));
    }
  }catch(e){toast('保存失败: '+e.message)}
}
async function testCloudflareEmailConnection(){
  const button=document.getElementById('btn_email_test');
  const body={
    cloudflare_api_base: _val('cloudflare_api_base'),
    cloudflare_admin_password: _val('cloudflare_admin_password'),
    cloudflare_domain: _val('cloudflare_domain'),
    cloudflare_site_password: _val('cloudflare_site_password'),
  };
  if(button) button.disabled=true;
  try{
    const j=await api('/api/config/email/test',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)
    });
    toast(j.message||'邮箱服务连接成功');
    setEmailHint(j.message||'Cloudflare Temp Email 连接成功');
  }catch(e){
    toast('连接失败: '+e.message);
    setEmailHint('连接失败: '+e.message);
  }finally{
    if(button) button.disabled=false;
  }
}
function setEmailHint(text){
  const el=document.getElementById('email_hint');
  if(!el) return;
  const t=String(text||'').trim();
  el.textContent=t;
  el.style.display=t ? '' : 'none';
}
function toggleSelectAllFiles(on){
  const boxes=document.querySelectorAll('.chk-file');
  boxes.forEach(b=>{ b.checked=!!on; });
  const all=document.getElementById('chk_all_files');
  if(all) all.checked=!!on;
}
async function deleteSelectedFiles(){
  const files=[...document.querySelectorAll('.chk-file:checked')].map(b=>b.value);
  if(!files.length){
    toast('请先勾选要删除的账号文件');
    return;
  }
  if(!confirm('确认删除选中的 '+files.length+' 个账号文件？\n删除后无法恢复，下载 SSO 时也不会再包含它们。')){
    return;
  }
  try{
    const j=await api('/api/accounts/delete',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({files})
    });
    toast(j.message||('已删除 '+((j.deleted||[]).length)+' 个文件'));
    setTimeout(()=>location.reload(), 500);
  }catch(e){toast('删除失败: '+e.message)}
}
async function loadBrowserEngine(){
  try{
    const j=await api('/api/config/browser');
    const eng=(j.browser_engine||'chromium').toLowerCase();
    document.getElementById('browser_engine').value=(eng==='camoufox'?'camoufox':'chromium');
  }catch(e){}
}
async function saveBrowserEngine(){
  const browser_engine=document.getElementById('browser_engine').value;
  try{
    const j=await api('/api/config/browser',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({browser_engine})});
    toast(j.message||('浏览器引擎: '+(j.browser_engine||browser_engine)));
  }catch(e){toast('保存浏览器引擎失败: '+e.message)}
}
async function startJob(){
  const count=parseInt(document.getElementById('count').value||'1',10);
  try{
    // auto-save email settings before start
    try{ await saveEmailConfig(); }catch(e){}
    try{ await saveBrowserEngine(); }catch(e){}
    const j=await api('/api/job/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({count, browser_engine: document.getElementById('browser_engine').value})});
    toast(j.message||'已启动');
    poll();
  }catch(e){toast('启动失败: '+e.message)}
}
async function stopJob(){
  try{
    const j=await api('/api/job/stop',{method:'POST'});
    toast(j.message||'已停止');
  }catch(e){toast('停止失败: '+e.message)}
}
async function backfillCpa(){
  try{
    const j=await api('/api/cpa/backfill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:200})});
    toast(j.message||('已入队 '+j.queued));
    poll();
  }catch(e){toast('补转失败: '+e.message)}
}
let lastLogLen=0;
async function poll(){
  try{
    const j=await api('/api/job/status');
    const st=j.job||{};
    const cpa=j.cpa||{};
    document.getElementById('st_status').textContent=st.status||'idle';
    document.getElementById('st_dot').className='dot'+(st.running?' run':'');
    document.getElementById('st_sf').textContent=`${st.success||0} / ${st.fail||0}`;
    document.getElementById('btn_start').disabled=!!st.running;
    if(document.getElementById('st_cpa_ok')){
      document.getElementById('st_cpa_ok').textContent=String(cpa.files||0);
    }
    if(document.getElementById('st_cpa_q')){
      document.getElementById('st_cpa_q').textContent=
        `${cpa.pending||0}待 / ${cpa.ok||0}成 / ${cpa.fail||0}败`;
    }
    if(document.getElementById('cpa_hint')){
      const core = cpa.core_ok ? 'core就绪' : ('core失败: '+(cpa.core_error||''));
      const last = cpa.last_ok_email ? (' · 最近OK: '+cpa.last_ok_email) : '';
      const err = cpa.last_error ? (' · 最近错: '+cpa.last_error) : '';
      document.getElementById('cpa_hint').textContent =
        `代理走本机 Clash · 自动CPA: ${cpa.enabled?'开':'关'} · ${core} · 文件 ${cpa.files||0}${last}${err}`;
    }
    const box=document.getElementById('logbox');
    const logs=j.logs||[];
    if(logs.length!==lastLogLen){
      box.textContent=logs.join('\n');
      box.scrollTop=box.scrollHeight;
      lastLogLen=logs.length;
    }
  }catch(e){}
}
loadEmailConfig();
loadBrowserEngine();
poll();
setInterval(poll, 2000);
</script>
</body></html>
"""

PREVIEW_HTML = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>预览 {{ name }}</title>
<style>
:root{--bg:#0b0e14;--card:#141a26;--fg:#eef2fb;--muted:#8b97b0;--line:#222b3d;--accent:#6ea8fe;--accent2:#4f8cff;--bg2:#0f131c}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  background:radial-gradient(1000px 500px at 12% -18%,#1a2540 0%,transparent 55%),var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 56px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;padding-bottom:16px;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:10px}
.top a{color:var(--accent);text-decoration:none;font-size:13.5px;padding:8px 14px;border:1px solid var(--line);border-radius:8px;transition:all .15s}
.top a:hover{border-color:var(--accent);background:rgba(110,168,254,.06)}
.brand{display:flex;align-items:center;gap:10px}
.logo{width:32px;height:32px;border-radius:9px;background:#000;display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:900;color:#fff;letter-spacing:-1px}
h1{margin:0;font-size:18px;font-weight:700}
pre{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:18px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace;font-size:13px;line-height:1.5;color:var(--muted)}
pre::-webkit-scrollbar{width:8px}
pre::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
</style>
</head><body><div class="wrap">
<div class="top">
  <div class="brand"><div class="logo">G</div><h1>{{ name }}</h1></div>
  <div><a href="/">← 返回</a> · <a href="/download/{{ name }}">下载</a></div>
</div>
<pre>{{ content }}</pre>
</div></body></html>
"""


# --------------- routes ---------------
@app.get("/login")
def login():
    # 默认无密码：直接进面板
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    if session.get("ok"):
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=None)


@app.post("/login")
def login_post():
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    if request.form.get("password") == PANEL_PASSWORD:
        session["ok"] = True
        return redirect(request.args.get("next") or url_for("index"))
    return render_template_string(LOGIN_HTML, error="密码错误"), 401


@app.get("/logout")
def logout():
    session.clear()
    if not PANEL_AUTH:
        return redirect(url_for("index"))
    return redirect(url_for("login"))


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/")
def index():
    need = require_login()
    if need:
        return need
    files_meta = []
    total = 0
    for p in list_account_files():
        lines = read_account_lines(p)
        total += len(lines)
        files_meta.append(
            {
                "name": p.name,
                "count": len(lines),
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return render_template_string(
        INDEX_HTML,
        base_dir=str(BASE_DIR),
        files=files_meta,
        file_count=len(files_meta),
        account_count=total,
        cpa_files=len(list_cpa_files()),
    )


def safe_name(name: str) -> Optional[Path]:
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not re.fullmatch(r"accounts_[\w.-]+\.txt", name):
        return None
    path = (BASE_DIR / name).resolve()
    if path.parent != BASE_DIR or not path.exists():
        return None
    return path


@app.get("/preview/<name>")
def preview_file(name: str):
    need = require_login()
    if need:
        return need
    path = safe_name(name)
    if not path:
        return "文件不存在", 404
    return render_template_string(
        PREVIEW_HTML,
        name=path.name,
        content=path.read_text(encoding="utf-8", errors="replace"),
    )


@app.get("/download/<name>")
def download_file(name: str):
    need = require_login()
    if need:
        return need
    path = safe_name(name)
    if not path:
        return "文件不存在", 404
    return send_file(
        path,
        as_attachment=True,
        download_name=path.name,
        mimetype="text/plain; charset=utf-8",
    )


def _merged_sso_txt() -> str:
    seen = set()
    lines = []
    for _, line in collect_all_accounts():
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


@app.get("/download/sso.txt")
def download_sso_txt():
    """主接口 1：全部 SSO，格式 email----password----sso"""
    need = require_login()
    if need:
        return need
    body = _merged_sso_txt()
    fname = f"sso_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/merged.txt")
def download_merged():
    """兼容旧链接 → 同 SSO txt"""
    return download_sso_txt()


@app.get("/download/all.zip")
def download_zip():
    need = require_login()
    if need:
        return need
    buf = io.BytesIO()
    files = list_account_files()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not files:
            zf.writestr("README.txt", "暂无 accounts_*.txt\n")
        for p in files:
            zf.write(p, arcname=p.name)
        seen = set()
        merged = []
        for _, line in collect_all_accounts():
            if line not in seen:
                seen.add(line)
                merged.append(line)
        zf.writestr(
            "accounts_merged_all.txt",
            "\n".join(merged) + ("\n" if merged else ""),
        )
    buf.seek(0)
    fname = f"accounts_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/accounts.json")
def download_accounts_json():
    """All accounts as one JSON array (email/password/sso)."""
    need = require_login()
    if need:
        return need
    accounts = unique_accounts()
    body = json.dumps(accounts, ensure_ascii=False, indent=2) + "\n"
    fname = f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/cpa.zip")
def download_cpa_zip():
    """主接口 2：已自动 OAuth 转换的真 CPA JSON（auth_kind=oauth）。"""
    need = require_login()
    if need:
        return need
    files = list_cpa_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → 真 CPA (CLIProxyAPI) JSON\n"
            "====================================\n\n"
            "1) 每个 xai-*.json 是 OAuth 凭证（access_token + refresh_token）。\n"
            "2) auth_kind=oauth，可直接放进 CLIProxyAPI auth-dir。\n"
            "3) 由注册成功后的 web SSO 自动换票生成。\n"
            "4) all.json 为全部账号数组；failed.jsonl 为转换失败记录（若有）。\n"
            "5) 若 zip 为空：先注册，或点「补转未转换 CPA」。\n"
        )
        zf.writestr("README.txt", readme)
        all_entries = []
        for i, p in enumerate(files, 1):
            try:
                raw = p.read_text(encoding="utf-8")
                obj = json.loads(raw)
                all_entries.append(obj)
                # keep original filename
                zf.writestr(p.name, raw if raw.endswith("\n") else raw + "\n")
            except Exception as e:
                zf.writestr(f"BAD-{p.name}.txt", str(e))
        zf.writestr(
            "all.json",
            json.dumps(all_entries, ensure_ascii=False, indent=2) + "\n",
        )
        if CPA_FAILED_PATH.exists():
            try:
                zf.write(CPA_FAILED_PATH, arcname="failed.jsonl")
            except Exception:
                pass
        if not files:
            zf.writestr(
                "EMPTY.txt",
                "暂无已转换的 CPA 文件。注册成功后会自动转换，或点击面板「补转未转换 CPA」。\n",
            )
    buf.seek(0)
    fname = f"cpa_oauth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


def _load_cpa_entries_for_sub2() -> Tuple[List[dict], List[str]]:
    """Read existing CPA JSON files for Sub2 export. No re-OAuth."""
    entries: List[dict] = []
    name_hints: List[str] = []
    for p in list_cpa_files():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(obj, dict):
                continue
            entries.append(obj)
            # xai-email.json → email hint; strip optional -fingerprint suffix
            stem = p.stem
            hint = stem[4:] if stem.lower().startswith("xai-") else stem
            name_hints.append(hint or "")
        except Exception:
            continue
    return entries, name_hints


def _fallback_sub2_payload(cpa_entries: List[dict], name_hints: List[str]) -> dict:
    """If sso2cpa_core import failed, still build a minimal sub2api-data package."""
    accounts: List[dict] = []
    for i, cpa in enumerate(cpa_entries):
        if not isinstance(cpa, dict):
            continue
        access = str(cpa.get("access_token") or "").strip()
        refresh = str(cpa.get("refresh_token") or "").strip()
        if not access and not refresh:
            continue
        email = str(cpa.get("email") or "").strip()
        sub = str(cpa.get("sub") or "").strip()
        hint = name_hints[i] if i < len(name_hints) else ""
        name = hint or email or sub or "grok-oauth"
        expires_at = str(cpa.get("expires_at") or cpa.get("expired") or "").strip()
        if not expires_at:
            expires_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        creds = {
            "access_token": access,
            "expires_at": expires_at,
            "base_url": str(cpa.get("base_url") or "https://cli-chat-proxy.grok.com/v1").strip(),
        }
        if refresh:
            creds["refresh_token"] = refresh
        token_type = str(cpa.get("token_type") or "Bearer").strip()
        if token_type:
            creds["token_type"] = token_type
        for k in ("id_token", "email", "sub", "client_id", "scope"):
            v = str(cpa.get(k) or "").strip()
            if v:
                creds[k] = v
        accounts.append(
            {
                "name": name,
                "platform": "grok",
                "type": "oauth",
                "credentials": creds,
                "concurrency": 1,
                "priority": 50,
            }
        )
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }


def _build_sub2_accounts(
    cpa_entries: List[dict], name_hints: List[str]
) -> List[dict]:
    """Map CPA entries → Sub2 DataAccount list (no re-OAuth)."""
    if build_sub2_payload is not None:
        payload = build_sub2_payload(cpa_entries, name_hints=name_hints)
        return list(payload.get("accounts") or [])
    payload = _fallback_sub2_payload(cpa_entries, name_hints)
    return list(payload.get("accounts") or [])


def _sub2_package(accounts: List[dict]) -> dict:
    """Official Sub2API import wrapper around account list."""
    if build_sub2_payload is not None:
        # reuse core helper for type/version/exported_at; pass empty CPA list
        # then inject accounts (avoids re-mapping)
        base = build_sub2_payload([])
        base["accounts"] = accounts
        return base
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": accounts,
    }


def _sub2_safe_arcname(name: str, used: Set[str]) -> str:
    """Unique zip member name: grok-{name}.json"""
    base = cpa_safe_filename(name or "grok-oauth")
    fname = f"grok-{base}.json"
    if fname not in used:
        used.add(fname)
        return fname
    i = 2
    while True:
        alt = f"grok-{base}-{i}.json"
        if alt not in used:
            used.add(alt)
            return alt
        i += 1


@app.get("/download/sub2.zip")
def download_sub2_zip():
    """主接口 3：Sub2API 官方导入包 ZIP（对齐 CPA zip 结构）。

    从已转换的 CPA JSON 现场映射，不重新注册/换票。

    zip 内容：
      README.txt
      grok-*.json     — 每个账号一份完整 sub2api-data（可单独导入）
      all.json        — 全部账号合集（推荐一键导入）
      EMPTY.txt       — 无账号时的说明
    """
    need = require_login()
    if need:
        return need
    cpa_entries, name_hints = _load_cpa_entries_for_sub2()
    accounts = _build_sub2_accounts(cpa_entries, name_hints)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Grok Register → Sub2API 官方导入包 (sub2api-data)\n"
            "================================================\n\n"
            "1) all.json：全部账号合集，推荐直接导入 Sub2API。\n"
            "   管理后台 → 账号 → 导入数据 → 上传 all.json\n"
            "2) grok-*.json：每个账号一份完整 sub2api-data（也可单独导入）。\n"
            "3) type=sub2api-data / version=1 / platform=grok / type=oauth\n"
            "4) 由已转换的 CPA OAuth 凭证现场映射，不重新注册/换票。\n"
            "5) proxies 为空；导入后请在 Sub2API 里绑定分组/代理。\n"
            "6) 若 zip 为空：先注册，或点面板「补转未转换 CPA」。\n"
        )
        zf.writestr("README.txt", readme)

        used_names: Set[str] = set()
        for acc in accounts:
            try:
                single = _sub2_package([acc])
                raw = json.dumps(single, ensure_ascii=False, indent=2) + "\n"
                arc = _sub2_safe_arcname(str(acc.get("name") or ""), used_names)
                zf.writestr(arc, raw)
            except Exception as e:
                bad = _sub2_safe_arcname(
                    f"BAD-{acc.get('name') or 'unknown'}", used_names
                )
                zf.writestr(bad.replace(".json", ".txt"), str(e))

        all_pkg = _sub2_package(accounts)
        zf.writestr(
            "all.json",
            json.dumps(all_pkg, ensure_ascii=False, indent=2) + "\n",
        )

        if not accounts:
            zf.writestr(
                "EMPTY.txt",
                "暂无已转换账号。注册成功后会自动转 CPA，再点「下载 Sub2」；"
                "或先点面板「补转未转换 CPA」。\n",
            )

    buf.seek(0)
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        buf, as_attachment=True, download_name=fname, mimetype="application/zip"
    )


@app.get("/download/sub2.json")
def download_sub2_json():
    """兼容旧链接：返回 all 合集 JSON（等同 zip 内 all.json）。"""
    need = require_login()
    if need:
        return need
    cpa_entries, name_hints = _load_cpa_entries_for_sub2()
    accounts = _build_sub2_accounts(cpa_entries, name_hints)
    payload = _sub2_package(accounts)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fname = f"sub2api_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/grok2api.json")
def download_grok2api_json():
    need = require_login()
    if need:
        return need
    body = (
        json.dumps(to_grok2api_pool(unique_accounts()), ensure_ascii=False, indent=2)
        + "\n"
    )
    fname = f"grok2api_pool_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/accounts")
def api_accounts():
    need = require_login()
    if need:
        return need
    data = []
    for source, line in collect_all_accounts():
        info = parse_line(line)
        info["source"] = source
        data.append(info)
    return jsonify(
        {
            "count": len(data),
            "files": [p.name for p in list_account_files()],
            "accounts": data,
        }
    )


@app.get("/api/nodes")
def api_nodes():
    need = require_login()
    if need:
        return need
    return jsonify(clash_list_nodes())


@app.post("/api/nodes/select")
def api_nodes_select():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    node = str(data.get("node") or "").strip()
    if not node:
        return jsonify({"ok": False, "error": "node required"}), 400
    ok, msg = clash_set_node(node)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg, "exit": clash_exit_ip()})


@app.post("/api/accounts/delete")
def api_accounts_delete():
    """Delete selected accounts_*.txt files (after user downloaded them)."""
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    names = data.get("files") or data.get("names") or []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names:
        return jsonify({"ok": False, "error": "files required"}), 400

    deleted = []
    missing = []
    errors = []
    for name in names:
        name = str(name or "").strip()
        path = safe_name(name)
        if not path:
            missing.append(name)
            continue
        try:
            path.unlink()
            deleted.append(path.name)
            log_line(f"[*] 已删除账号文件: {path.name}")
        except Exception as e:
            errors.append(f"{name}: {e}")

    if not deleted and errors:
        return jsonify({"ok": False, "error": "; ".join(errors)}), 400
    return jsonify(
        {
            "ok": True,
            "deleted": deleted,
            "missing": missing,
            "errors": errors,
            "message": f"已删除 {len(deleted)} 个文件"
            + (f"，跳过 {len(missing)}" if missing else ""),
        }
    )


@app.get("/api/config/email")
def api_get_email_config():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "email": email_config_public()})


@app.post("/api/config/email")
def api_set_email_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        email = apply_email_config_from_ui(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "message": "邮箱设置已保存", "email": email})


@app.post("/api/config/email/test")
def api_test_email_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        result = probe_cloudflare_temp_email(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(result)


@app.get("/api/job/status")
def api_job_status():
    need = require_login()
    if need:
        return need
    with _job_lock:
        job = dict(_job)
    return jsonify({"ok": True, "job": job, "logs": list(_logs), "cpa": cpa_stats()})


@app.get("/api/cpa/status")
def api_cpa_status():
    need = require_login()
    if need:
        return need
    return jsonify({"ok": True, "cpa": cpa_stats()})


@app.post("/api/cpa/backfill")
def api_cpa_backfill():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        limit = int(data.get("limit") or 200)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 1000))
    if not _CPA_CORE_OK:
        return jsonify({"ok": False, "error": f"core unavailable: {_CPA_CORE_ERR}"}), 500
    n = enqueue_missing_accounts(limit=limit)
    log_line(f"[CPA] 手动补转入队: {n}")
    return jsonify({"ok": True, "queued": n, "message": f"已入队 {n} 个待转换 SSO"})


def _normalize_browser_engine(value: str) -> str:
    eng = str(value or "").strip().lower()
    if eng in ("camoufox", "firefox", "headless", "cfox"):
        return "camoufox"
    return "chromium"


@app.get("/api/config/browser")
def api_get_browser_config():
    need = require_login()
    if need:
        return need
    cfg = load_config()
    return jsonify(
        {
            "ok": True,
            "browser_engine": _normalize_browser_engine(cfg.get("browser_engine") or "chromium"),
        }
    )


@app.post("/api/config/browser")
def api_set_browser_config():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    eng = _normalize_browser_engine(data.get("browser_engine") or "chromium")
    cfg = load_config()
    cfg["browser_engine"] = eng
    save_config(cfg)
    label = "Camoufox 无头" if eng == "camoufox" else "Chromium 有头"
    return jsonify(
        {
            "ok": True,
            "browser_engine": eng,
            "message": f"浏览器引擎已保存: {label}",
        }
    )


@app.post("/api/job/start")
def api_job_start():
    need = require_login()
    if need:
        return need
    data = request.get_json(force=True, silent=True) or {}
    try:
        count = int(data.get("count") or 1)
    except Exception:
        count = 1
    if "browser_engine" in data:
        eng = _normalize_browser_engine(data.get("browser_engine"))
        cfg = load_config()
        cfg["browser_engine"] = eng
        save_config(cfg)
    ok, msg = start_job(count)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


@app.post("/api/job/stop")
def api_job_stop():
    need = require_login()
    if need:
        return need
    ok, msg = stop_job()
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "message": msg})


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "base_dir": str(BASE_DIR),
            "files": len(list_account_files()),
            "running": bool(_job.get("running")),
            "cpa": cpa_stats(),
        }
    )


# start background CPA worker when module loads (systemd imports/runs this file)
start_cpa_worker()


if __name__ == "__main__":
    print(f"Grok Register Panel -> http://0.0.0.0:{PORT}")
    print(f"CPA auto-convert dir -> {CPA_DIR} core={_CPA_CORE_OK}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
