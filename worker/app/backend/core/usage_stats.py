"""Privacy-safe, offline-tolerant usage statistics for TC01-TC06.

The render UI only enqueues small scalar lifecycle events.  A daemon thread owns the
SQLite outbox and all network I/O so statistics can never delay or change a
render verdict.  Failed deliveries stay in the outbox and are retried with
bounded exponential backoff the next time the dispatcher is active.

Lifecycle schema v2-v6 emits one ``RUN_STARTED`` event after ``Worker.start``
has accepted a top-level render and one ``RUN_FINISHED`` event from its
canonical ``PipelineResult``.  Both events share a random, local
``client_run_id`` and use deterministic idempotency keys.  Schema v3 adds one
validated desktop identity snapshot; missing or invalid identity falls back to
schema v2 and never disables installation-key telemetry.  Schemas v4-v6 add
strictly allow-listed run metrics, coarse hardware capabilities, and bounded
resource samples while preserving the same auto-enrollment and local outbox.

Configuration is optional.  It is read from ``~/.green_pc/greenstats.json``
and can be overridden with ``GREENPC_STATS_*`` environment variables.  With
no endpoint (or when disabled), events remain local and the app works fully
offline.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import ipaddress
import unicodedata
import urllib.request
import uuid
from contextlib import closing
from urllib.error import HTTPError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse, urlunparse


SUPPORTED_TCS = frozenset({"TC01", "TC02", "TC03", "TC04", "TC05", "TC06"})
LEGACY_SUPPORTED_TCS = frozenset({"TC01", "TC02", "TC03", "TC04"})
TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "PAUSED", "INVALID_INPUT"}
)
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CLIENT_RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_APP_VERSION_RE = re.compile(r"^V?(\d+\.\d+\.\d+\.\d+)$", re.IGNORECASE)
_USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IDENTITY_TYPES = frozenset({"email", "phone", "user_id"})
_CLIENT_INSTALLATION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_ENROLLMENT_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43,64}$")
_API_TOKEN_RE = re.compile(r"^gsk_[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$")
_SERVER_INSTALLATION_ID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
)
_ENCODERS = frozenset(
    {
        "auto",
        "libx264",
        "h264_nvenc",
        "hevc_nvenc",
        "av1_nvenc",
        "h264_qsv",
        "h264_amf",
        "h264_videotoolbox",
        "hevc_videotoolbox",
    }
)
_ENV_PREFIX = "GREENPC_STATS_"
_PRODUCTION_ENDPOINT = "https://greenstats.sj88ai.com/api/v1/ingest/runs"
_DEFAULT_APP_VERSION = "V1.0.2.17"
_ENROLLMENT_PROTOCOL_VERSION = 1
_LEGACY_SCHEMA_VERSION = 1
_LIFECYCLE_SCHEMA_VERSION = 2
_IDENTITY_SCHEMA_VERSION = 3
_METRICS_SCHEMA_VERSION = 4
_HARDWARE_SCHEMA_VERSION = 5
_RESOURCE_SCHEMA_VERSION = 6
_COARSE_OS_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_SAFE_GPU_MODEL_RE = re.compile(r"^[A-Za-z0-9 ._+()-]{1,80}$")
_RAW_IDENTIFIER_RE = re.compile(
    r"(?:[0-9a-f]{12,}|\b(?:serial|uuid|device[ _-]?id|mac[ _-]?address|hostname)\b)",
    re.IGNORECASE,
)
_COARSE_CPU_MODELS = frozenset(
    {
        "Apple Silicon",
        "Intel",
        "Intel Xeon",
        "Intel Core i3",
        "Intel Core i5",
        "Intel Core i7",
        "Intel Core i9",
        "AMD",
        "AMD EPYC",
        "AMD Ryzen 3",
        "AMD Ryzen 5",
        "AMD Ryzen 7",
        "AMD Ryzen 9",
        "ARM",
    }
)
_STAGE_IDS = frozenset(
    {"reframe", "chroma", "batch_chroma", "audio_master", "unknown"}
)
_STAGE_ORDER = ("reframe", "chroma", "batch_chroma", "audio_master", "unknown")
_STAGE_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "CANCELLED",
        "PAUSED",
        "INVALID_INPUT",
        "UNKNOWN",
    }
)
_RUN_STARTED = "RUN_STARTED"
_RUN_FINISHED = "RUN_FINISHED"
_DELIVERED = "delivered"
_RETRY = "retry"
_PERMANENT = "permanent"
_DEFAULT_MAX_OUTBOX_EVENTS = 5_000
_DEFAULT_MAX_OUTBOX_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_EVENT_AGE_SEC = 30 * 24 * 60 * 60
_DEFAULT_MAX_QUARANTINE_EVENTS = 1_000
_DEFAULT_MAX_QUARANTINE_AGE_SEC = 30 * 24 * 60 * 60
_MAINTENANCE_INTERVAL_SEC = 5 * 60.0


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward installation credentials through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirectHandler())


def _urlopen_without_redirect(request: Any, *, timeout: float) -> Any:
    """Open exactly the configured endpoint; redirects surface as HTTPError."""

    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass(frozen=True)
class StatsIdentity:
    """Validated, normalized identity used by exactly one telemetry run."""

    identity_type: str
    identity_value: str

    @property
    def masked(self) -> str:
        return mask_identity(self)

    def as_payload(self) -> dict[str, str]:
        return {
            "identity_type": self.identity_type,
            "identity_value": self.identity_value,
        }


def normalize_identity(identity_type: Any, identity_value: Any) -> StatsIdentity:
    """Validate and normalize one v0.4 identity or raise ``ValueError``.

    Raw values are intentionally returned only to the local config/outbox path.
    UI surfaces should use :func:`mask_identity` instead.
    """

    selected = str(identity_type or "").strip().casefold()
    if selected not in _IDENTITY_TYPES:
        raise ValueError("ประเภทข้อมูลผู้ใช้ต้องเป็น email, phone หรือ user_id")

    value = unicodedata.normalize("NFKC", str(identity_value or "")).strip()
    if selected == "email":
        value = value.casefold()
        if len(value) > 254 or value.count("@") != 1:
            raise ValueError("รูปแบบอีเมลไม่ถูกต้อง")
        local, domain = value.rsplit("@", 1)
        if (
            not local
            or len(local) > 64
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in local)
            or any(ch in '()<>[]:;\\,"' for ch in local)
        ):
            raise ValueError("รูปแบบอีเมลไม่ถูกต้อง")
        try:
            domain_ascii = domain.encode("idna").decode("ascii").casefold()
        except (UnicodeError, ValueError):
            raise ValueError("โดเมนอีเมลไม่ถูกต้อง") from None
        labels = domain_ascii.split(".")
        if len(labels) < 2 or any(
            not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels
        ):
            raise ValueError("โดเมนอีเมลต้องมีอย่างน้อย 2 ส่วน")
        value = f"{local}@{domain_ascii}"
        if len(value) > 254:
            raise ValueError("อีเมลยาวเกิน 254 ตัวอักษร")
    elif selected == "phone":
        compact = "".join(
            ch for ch in value if not ch.isspace() and ch not in "()-."
        )
        if compact.startswith("00"):
            compact = "+" + compact[2:]
        if not _PHONE_RE.fullmatch(compact):
            raise ValueError("เบอร์โทรต้องเป็น E.164 เช่น +66812345678")
        value = compact
    else:
        value = value.casefold()
        if not _USER_ID_RE.fullmatch(value):
            raise ValueError(
                "user_id ต้องยาว 3-64 ตัว และใช้ a-z, 0-9, จุด, ขีดกลางหรือขีดล่าง"
            )
    return StatsIdentity(selected, value)


def mask_identity(identity: StatsIdentity) -> str:
    """Return a stable local preview that never reveals the complete value."""

    if not isinstance(identity, StatsIdentity):
        return ""
    value = identity.identity_value
    if identity.identity_type == "email":
        local, domain = value.rsplit("@", 1)
        labels = domain.split(".")
        masked_domain = f"{labels[0][:1]}***"
        if len(labels) > 1:
            masked_domain += "." + ".".join(labels[1:])
        return f"{local[:1]}***@{masked_domain}"
    if identity.identity_type == "phone":
        digits = value.lstrip("+")
        return "*" * max(0, len(digits) - 4) + digits[-4:]
    return f"{value[:1]}***{value[-2:]}"


def normalize_telemetry_identity(
    identity_type: Any,
    identity_value: Any,
) -> Optional[tuple[str, str]]:
    """Compatibility adapter for the v3-v6 telemetry builders.

    The desktop configuration keeps the V1.0.2.17 ``StatsIdentity`` contract;
    newer schema builders consume the equivalent tuple form.
    """

    try:
        identity = normalize_identity(identity_type, identity_value)
    except (TypeError, ValueError):
        return None
    return identity.identity_type, identity.identity_value


def mask_telemetry_identity(identity_type: Any, identity_value: Any) -> str:
    """Return the existing desktop-safe mask through the tuple API."""

    normalized = normalize_telemetry_identity(identity_type, identity_value)
    if normalized is None:
        return ""
    return mask_identity(StatsIdentity(*normalized))


def _enrollment_endpoint(endpoint: str) -> str:
    """Return a same-origin enrollment URL or an empty string when unsafe."""

    parsed = urlparse(str(endpoint or "").strip())
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/api/v1/ingest", "/api/v1/ingest/runs"}
    ):
        return ""
    if parsed.scheme != "https":
        if parsed.scheme != "http":
            return ""
        hostname = str(parsed.hostname or "").strip().casefold()
        try:
            loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return ""
    return urlunparse(
        (parsed.scheme, parsed.netloc, "/api/v1/installations/enroll", "", "", "")
    )


@dataclass(frozen=True)
class StatsConfig:
    enabled: bool = False
    endpoint: str = ""
    token: str = ""
    token_header: str = "Authorization"
    token_prefix: str = "Bearer"
    timeout_sec: float = 2.5
    retry_initial_sec: float = 30.0
    retry_max_sec: float = 3600.0
    identity_type: str = ""
    identity_value: str = ""
    client_installation_id: str = ""
    enrollment_secret: str = ""
    max_outbox_events: int = _DEFAULT_MAX_OUTBOX_EVENTS
    max_outbox_bytes: int = _DEFAULT_MAX_OUTBOX_BYTES
    max_event_age_sec: float = _DEFAULT_MAX_EVENT_AGE_SEC
    max_quarantine_events: int = _DEFAULT_MAX_QUARANTINE_EVENTS
    max_quarantine_age_sec: float = _DEFAULT_MAX_QUARANTINE_AGE_SEC

    @property
    def identity(self) -> Optional[StatsIdentity]:
        try:
            return normalize_identity(self.identity_type, self.identity_value)
        except (TypeError, ValueError):
            return None

    @property
    def can_send(self) -> bool:
        # A URL is safe to bundle; an installation credential is not. Missing
        # provisioning therefore fails closed even when enabled is true.
        if not self.enabled or not self.endpoint or not self.token:
            return False
        parsed = urlparse(self.endpoint)
        if not parsed.netloc or parsed.username or parsed.password:
            return False
        if parsed.scheme == "https":
            return True
        if parsed.scheme != "http":
            return False
        hostname = str(parsed.hostname or "").strip().casefold()
        if hostname == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    @property
    def can_enroll(self) -> bool:
        return bool(
            self.enabled
            and not self.token
            and _CLIENT_INSTALLATION_ID_RE.fullmatch(self.client_installation_id)
            and _ENROLLMENT_SECRET_RE.fullmatch(self.enrollment_secret)
            and _enrollment_endpoint(self.endpoint)
        )


def default_data_dir() -> Path:
    override = os.environ.get(f"{_ENV_PREFIX}DATA_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".green_pc"


def _config_path(
    *,
    data_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    env = os.environ if environ is None else environ
    if data_dir is None:
        override = str(env.get(f"{_ENV_PREFIX}DATA_DIR", "") or "").strip()
        root = Path(override).expanduser() if override else Path.home() / ".green_pc"
    else:
        root = Path(data_dir)
    configured = str(env.get(f"{_ENV_PREFIX}CONFIG", "") or "").strip()
    return Path(configured).expanduser() if configured else root / "greenstats.json"


def load_config(
    *,
    data_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> StatsConfig:
    """Read fail-closed configuration without filesystem or network effects."""
    env = os.environ if environ is None else environ
    path = _config_path(data_dir=data_dir, environ=env)

    raw: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        raw = {}

    def value(name: str, default: Any = "") -> Any:
        env_name = f"{_ENV_PREFIX}{name.upper()}"
        return env[env_name] if env_name in env else raw.get(name, default)

    token = str(value("token", "") or "").strip()
    endpoint = str(value("endpoint", "") or "").strip() or _PRODUCTION_ENDPOINT
    enabled_default = bool(token)
    enabled = _truthy(value("enabled", enabled_default), enabled_default)
    token_header = str(value("token_header", "Authorization") or "Authorization").strip()
    if not _HEADER_RE.fullmatch(token_header):
        token_header = "Authorization"
    try:
        identity = normalize_identity(
            value("identity_type", ""), value("identity_value", "")
        )
    except (TypeError, ValueError):
        identity = None
    return StatsConfig(
        enabled=enabled,
        endpoint=endpoint,
        token=token,
        token_header=token_header,
        token_prefix=str(value("token_prefix", "Bearer") or "").strip(),
        timeout_sec=_bounded_float(value("timeout_sec", 2.5), 2.5, 0.25, 10.0),
        retry_initial_sec=_bounded_float(
            value("retry_initial_sec", 30.0), 30.0, 1.0, 3600.0
        ),
        retry_max_sec=_bounded_float(
            value("retry_max_sec", 3600.0), 3600.0, 1.0, 86400.0
        ),
        identity_type=identity.identity_type if identity else "",
        identity_value=identity.identity_value if identity else "",
        client_installation_id=str(value("client_installation_id", "") or "").strip(),
        enrollment_secret=str(value("enrollment_secret", "") or "").strip(),
        max_outbox_events=_bounded_int(
            value("max_outbox_events", _DEFAULT_MAX_OUTBOX_EVENTS),
            _DEFAULT_MAX_OUTBOX_EVENTS,
            100,
            100_000,
        ),
        max_outbox_bytes=_bounded_int(
            value("max_outbox_bytes", _DEFAULT_MAX_OUTBOX_BYTES),
            _DEFAULT_MAX_OUTBOX_BYTES,
            64 * 1024,
            256 * 1024 * 1024,
        ),
        max_event_age_sec=_bounded_float(
            value("max_event_age_sec", _DEFAULT_MAX_EVENT_AGE_SEC),
            float(_DEFAULT_MAX_EVENT_AGE_SEC),
            3600.0,
            365.0 * 24.0 * 60.0 * 60.0,
        ),
        max_quarantine_events=_bounded_int(
            value("max_quarantine_events", _DEFAULT_MAX_QUARANTINE_EVENTS),
            _DEFAULT_MAX_QUARANTINE_EVENTS,
            10,
            10_000,
        ),
        max_quarantine_age_sec=_bounded_float(
            value("max_quarantine_age_sec", _DEFAULT_MAX_QUARANTINE_AGE_SEC),
            float(_DEFAULT_MAX_QUARANTINE_AGE_SEC),
            3600.0,
            365.0 * 24.0 * 60.0 * 60.0,
        ),
    )


def bootstrap_temp_identity(
    *,
    data_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> StatsIdentity:
    """Ensure the local config has a usable identity.

    Runs ``load_config`` and, if no identity is configured, generates a
    fresh Thai-style nickname (``<adj>-<animal>-<NN>``) and writes it back
    to ``greenstats.json`` atomically.  Returns the resulting identity.
    """
    cfg = load_config(data_dir=data_dir, environ=environ)
    if cfg.identity and (cfg.identity.identity_type and cfg.identity.identity_value):
        return cfg.identity
    try:
        from core.auto_identity import generate_temp_nickname
    except Exception:
        identity_type, identity_value = "user_id", "anon-user-00"
    else:
        identity_type, identity_value = generate_temp_nickname()
    return save_identity_config(
        identity_type=identity_type,
        identity_value=identity_value,
        data_dir=data_dir,
        environ=environ,
    )


def save_identity_config(
    identity_type: Any,
    identity_value: Any,
    *,
    data_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> StatsIdentity:
    """Atomically save only desktop identity fields in ``greenstats.json``.

    Existing provisioning fields (endpoint, token, retry/retention settings)
    and an existing POSIX permission mode are preserved.  Malformed config is
    never overwritten because doing so could destroy administrator settings.
    """

    identity = normalize_identity(identity_type, identity_value)
    path = _config_path(data_dir=data_dir, environ=environ)
    current: dict[str, Any] = {}
    existing_mode: Optional[int] = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("ไฟล์ตั้งค่า GreenStats เสีย จึงยังไม่บันทึกทับ") from exc
        if not isinstance(loaded, dict):
            raise ValueError("ไฟล์ตั้งค่า GreenStats ต้องเป็น JSON object")
        current = loaded
        try:
            existing_mode = path.stat().st_mode & 0o777
        except OSError:
            existing_mode = None

    current["identity_type"] = identity.identity_type
    current["identity_value"] = identity.identity_value
    serialized = json.dumps(
        current, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"

    parent_existed = path.parent.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt" and not parent_existed:
            os.chmod(path.parent, 0o700)
    except OSError as exc:
        raise OSError("สร้างโฟลเดอร์ตั้งค่าข้อมูลผู้ใช้ไม่ได้") from exc

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    mode = existing_mode if existing_mode is not None else 0o600
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd: Optional[int] = None
    try:
        fd = os.open(str(temp_path), flags, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = None
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, mode)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return identity


def generate_client_installation_id() -> str:
    """Return an opaque random installation ID with no hostname/device data."""

    return uuid.uuid4().hex


def bootstrap_device_config(
    *,
    data_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[StatsConfig]:
    """Create retry-stable enrollment material without exposing device data."""
    env = os.environ if environ is None else environ
    path = _config_path(data_dir=data_dir, environ=env)
    # An operator-provided credential always wins.  In particular, never copy
    # an environment secret into greenstats.json while performing bootstrap.
    if str(env.get(f"{_ENV_PREFIX}TOKEN", "") or "").strip():
        return None
    existing: dict[str, Any] = {}
    existing_mode = 0o600
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return None
            existing = loaded
            existing_mode = path.stat().st_mode & 0o777 or 0o600
        except Exception:
            return None
        if str(existing.get("token", "") or "").strip():
            return None
        legacy_device_identity = (
            str(existing.get("identity_type", "") or "").strip().casefold()
            == "device"
        )
        # Respect an explicit operator opt-out.  The only exception is the
        # broken V1.0.2.15 bootstrap file: it always wrote enabled=false plus a
        # legacy device identity even though the user had not disabled stats.
        if (
            "enabled" in existing
            and not _truthy(existing.get("enabled"), False)
            and not legacy_device_identity
        ):
            return None
        if (
            _CLIENT_INSTALLATION_ID_RE.fullmatch(
                str(existing.get("client_installation_id", "") or "").strip()
            )
            and _ENROLLMENT_SECRET_RE.fullmatch(
                str(existing.get("enrollment_secret", "") or "").strip()
            )
        ):
            return None

    endpoint = (
        str(env.get(f"{_ENV_PREFIX}ENDPOINT", "") or "").strip()
        or str(existing.get("endpoint", "") or "").strip()
        or _PRODUCTION_ENDPOINT
    )
    client_installation_id = generate_client_installation_id()
    enrollment_secret = secrets.token_urlsafe(32)
    try:
        identity = normalize_identity(
            existing.get("identity_type", ""), existing.get("identity_value", "")
        )
    except (TypeError, ValueError):
        identity = None
    config = StatsConfig(
        enabled=True,
        endpoint=endpoint,
        token="",
        identity_type=identity.identity_type if identity else "",
        identity_value=identity.identity_value if identity else "",
        client_installation_id=client_installation_id,
        enrollment_secret=enrollment_secret,
    )
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: Optional[int] = None
    try:
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt" and not parent_existed:
            os.chmod(path.parent, 0o700)
        payload = dict(existing)
        payload.update({
            "enabled": config.enabled,
            "endpoint": config.endpoint,
            "token": config.token,
            "token_header": config.token_header,
            "token_prefix": config.token_prefix,
            "identity_type": config.identity_type,
            "identity_value": config.identity_value,
            "client_installation_id": config.client_installation_id,
            "enrollment_secret": config.enrollment_secret,
        })
        serialized = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        fd = os.open(
            str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, existing_mode
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = None
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, existing_mode)
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return config


def _persist_enrollment_token(
    config: StatsConfig,
    *,
    token: str,
    server_installation_id: str,
    data_dir: Path,
) -> bool:
    """Atomically bind one server credential to its original local seed."""

    if not _API_TOKEN_RE.fullmatch(token) or not _SERVER_INSTALLATION_ID_RE.fullmatch(
        server_installation_id
    ):
        return False
    path = _config_path(data_dir=data_dir)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return False
        if str(loaded.get("client_installation_id", "")) != config.client_installation_id:
            return False
        if str(loaded.get("enrollment_secret", "")) != config.enrollment_secret:
            return False
        current_token = str(loaded.get("token", "") or "").strip()
        if current_token and current_token != token:
            return False
        existing_mode = path.stat().st_mode & 0o777
    except Exception:
        return False

    loaded["enabled"] = True
    loaded["token"] = token
    loaded["server_installation_id"] = server_installation_id
    serialized = json.dumps(
        loaded, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd: Optional[int] = None
    try:
        fd = os.open(str(temp_path), flags, existing_mode or 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = None
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temp_path, existing_mode or 0o600)
        os.replace(temp_path, path)
        if os.name != "nt":
            os.chmod(path, existing_mode or 0o600)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _desktop_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def configured_identity_snapshot(
    *,
    data_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[dict[str, str]]:
    """Read one normalized identity snapshot; invalid/missing config is None."""

    try:
        identity = load_config(data_dir=data_dir, environ=environ).identity
        return identity.as_payload() if identity is not None else None
    except Exception:
        return None


def _status_from_record(record: Mapping[str, Any]) -> str:
    pipeline = record.get("pipeline_result")
    if isinstance(pipeline, Mapping):
        value = str(pipeline.get("status", "") or "").strip().upper()
        if value in TERMINAL_STATUSES:
            return value
    fallback = str(record.get("status", "") or "").strip().casefold()
    return {
        "done": "SUCCEEDED",
        "succeeded": "SUCCEEDED",
        "partial": "PARTIAL",
        "failed": "FAILED",
        "cancelled": "CANCELLED",
        "paused": "PAUSED",
        "invalid_input": "INVALID_INPUT",
    }.get(fallback, "FAILED")


def _sequence_count(value: Any) -> int:
    if not isinstance(value, (list, tuple)):
        return 0
    return len(value)


def _tc04_final_stage_counts(
    pipeline: Mapping[str, Any],
) -> Optional[tuple[int, Optional[int]]]:
    """Return newly produced/final-plan counts from TC04's chroma stage.

    TC04 may deliberately surface valid reframe files in the top-level
    ``PipelineResult`` when the final batch/chroma stage is skipped.  Those
    files are useful UI diagnostics, but they are intermediate artifacts and
    must never be counted as telemetry outputs.  A present stage list is
    therefore fail-closed: only ``batch_chroma`` can contribute TC04 counts.
    """

    stages = pipeline.get("stages")
    if not isinstance(stages, (list, tuple)) or not stages:
        return None
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        if str(stage.get("name", "") or "").strip().casefold() != "batch_chroma":
            continue
        produced = stage.get("produced_this_run")
        if not _is_nonnegative_int(produced):
            produced = stage.get("succeeded")
        output_count = produced if _is_nonnegative_int(produced) else 0
        expected = stage.get("expected")
        expected_output_count = expected if _is_nonnegative_int(expected) else None
        return output_count, expected_output_count
    # TC04 stage data exists but has no canonical final stage.  Do not fall
    # back to top-level counts because they may describe surfaced reframes.
    return 0, None


def _normalize_app_version(value: Any) -> str:
    match = _APP_VERSION_RE.fullmatch(str(value or "").strip())
    return f"V{match.group(1)}" if match else ""


def _read_packaged_app_version() -> str:
    candidates: list[Path] = []
    mei_root = str(getattr(sys, "_MEIPASS", "") or "").strip()
    if mei_root:
        candidates.append(Path(mei_root) / "version.txt")
    candidates.append(Path(__file__).resolve().parents[1] / "version.txt")
    patterns = (
        re.compile(r"ProductVersion[^0-9]+(\d+\.\d+\.\d+\.\d+)", re.IGNORECASE),
        re.compile(r"\bV?(\d+\.\d+\.\d+\.\d+)\b", re.IGNORECASE),
    )
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                version = _normalize_app_version(match.group(1))
                if version:
                    return version
    return "V0.0.0.0"


# Resolve once during module import, never in the Tk render-completion callback.
_PACKAGED_APP_VERSION = _read_packaged_app_version()


def _app_version_from_record(record: Mapping[str, Any]) -> str:
    direct = _normalize_app_version(record.get("app_version"))
    if direct:
        return direct
    pipeline = record.get("pipeline_result")
    if isinstance(pipeline, Mapping):
        direct = _normalize_app_version(pipeline.get("app_version"))
        if direct:
            return direct
        metadata = pipeline.get("metadata")
        if isinstance(metadata, Mapping):
            direct = _normalize_app_version(metadata.get("app_version"))
            if direct:
                return direct
    # Frozen builds expose the already-loaded entrypoint as ``__main__``.
    # Reading that scalar avoids importing main.py (and avoids any file I/O in
    # the Tk completion callback) when version.txt is not bundled as data.
    main_module = sys.modules.get("__main__")
    direct = _normalize_app_version(getattr(main_module, "APP_VERSION", ""))
    if direct:
        return direct
    return _PACKAGED_APP_VERSION


def _valid_timestamp(value: Any) -> bool:
    try:
        occurred_at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return occurred_at.tzinfo is not None


def _utc_timestamp(value: Any = None) -> str:
    if isinstance(value, datetime):
        occurred_at = value
    else:
        try:
            occurred_at = datetime.fromisoformat(
                str(value or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            occurred_at = datetime.now(timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bounded_int_or_none(value: Any, low: int, high: int) -> Optional[int]:
    try:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            return None
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _valid_common_lifecycle(payload: Mapping[str, Any], event_type: str) -> bool:
    client_run_id = str(payload.get("client_run_id", "") or "")
    return bool(
        payload.get("schema_version")
        in {
            _LIFECYCLE_SCHEMA_VERSION,
            _IDENTITY_SCHEMA_VERSION,
            _METRICS_SCHEMA_VERSION,
            _HARDWARE_SCHEMA_VERSION,
            _RESOURCE_SCHEMA_VERSION,
        }
        and payload.get("event_type") == event_type
        and _CLIENT_RUN_ID_RE.fullmatch(client_run_id)
        and payload.get("event_id")
        == f"{client_run_id}-{'start' if event_type == _RUN_STARTED else 'finish'}"
        and _EVENT_ID_RE.fullmatch(str(payload.get("event_id", "")))
        and payload.get("tc_id") in SUPPORTED_TCS
        and _normalize_app_version(payload.get("app_version"))
        == payload.get("app_version")
        and _valid_timestamp(payload.get("occurred_at"))
        and _is_nonnegative_int(payload.get("input_count"))
        and payload["input_count"] <= 10_000_000
    )


def _identity_fields(payload: Mapping[str, Any]) -> set[str]:
    if payload.get("schema_version") == _IDENTITY_SCHEMA_VERSION:
        return {"identity_type", "identity_value"}
    if payload.get("schema_version") in {
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    } and (
        "identity_type" in payload or "identity_value" in payload
    ):
        return {"identity_type", "identity_value"}
    return set()


def _valid_identity_payload(payload: Mapping[str, Any]) -> bool:
    schema_version = payload.get("schema_version")
    if schema_version == _LIFECYCLE_SCHEMA_VERSION:
        return "identity_type" not in payload and "identity_value" not in payload
    if schema_version in {
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    } and (
        "identity_type" not in payload and "identity_value" not in payload
    ):
        return True
    if schema_version not in {
        _IDENTITY_SCHEMA_VERSION,
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    }:
        return False
    normalized = normalize_telemetry_identity(
        payload.get("identity_type"), payload.get("identity_value")
    )
    return bool(
        normalized is not None
        and normalized[0] == payload.get("identity_type")
        and normalized[1] == payload.get("identity_value")
    )


def _strict_optional_int(value: Any, low: int, high: int) -> bool:
    return value is None or (
        _is_nonnegative_int(value) and low <= value <= high
    )


def _strict_optional_number(value: Any, low: float, high: float) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and low <= float(value) <= high
    )


def _valid_v4_profile(profile: Any) -> bool:
    if not isinstance(profile, Mapping) or set(profile) != {
        "encoder",
        "width",
        "height",
        "fps",
        "parallel_workers",
        "cpu_percent",
    }:
        return False
    encoder = profile.get("encoder")
    return bool(
        (encoder is None or encoder in _ENCODERS)
        and _strict_optional_int(profile.get("width"), 1, 16_384)
        and _strict_optional_int(profile.get("height"), 1, 16_384)
        and _strict_optional_int(profile.get("fps"), 1, 240)
        and _strict_optional_int(profile.get("parallel_workers"), 1, 256)
        and _strict_optional_int(profile.get("cpu_percent"), 0, 100)
    )


def _valid_v4_metrics(payload: Mapping[str, Any]) -> bool:
    media = payload.get("media")
    if not isinstance(media, Mapping) or set(media) != {
        "input_duration_ms",
        "produced_duration_ms",
    }:
        return False
    if not all(
        _strict_optional_int(media.get(key), 0, 31_536_000_000_000)
        for key in ("input_duration_ms", "produced_duration_ms")
    ):
        return False

    resources = payload.get("resources")
    resource_values = (
        "cpu_avg_percent",
        "cpu_peak_percent",
        "ram_avg_mb",
        "ram_peak_mb",
    )
    if not isinstance(resources, Mapping) or set(resources) != {
        "sample_count",
        *resource_values,
    }:
        return False
    sample_count = resources.get("sample_count")
    if not _is_nonnegative_int(sample_count) or sample_count > 100_000_000:
        return False
    values = [resources.get(key) for key in resource_values]
    if (sample_count == 0 and any(value is not None for value in values)) or (
        sample_count > 0 and any(value is None for value in values)
    ):
        return False
    if not all(
        _strict_optional_number(resources.get(key), 0.0, 100.0)
        for key in ("cpu_avg_percent", "cpu_peak_percent")
    ) or not all(
        _strict_optional_number(resources.get(key), 0.0, 1_048_576.0)
        for key in ("ram_avg_mb", "ram_peak_mb")
    ):
        return False
    if sample_count > 0 and (
        float(resources["cpu_avg_percent"]) > float(resources["cpu_peak_percent"])
        or float(resources["ram_avg_mb"]) > float(resources["ram_peak_mb"])
    ):
        return False

    encoding = payload.get("encoding")
    actual_encoders = (_ENCODERS - {"auto"}) | {"mixed"}
    if not isinstance(encoding, Mapping) or set(encoding) != {
        "actual_encoder",
        "fallback_used",
    }:
        return False
    if encoding.get("actual_encoder") not in actual_encoders | {None}:
        return False
    if not isinstance(encoding.get("fallback_used"), bool):
        return False
    if encoding["fallback_used"] and encoding.get("actual_encoder") is None:
        return False

    failure = payload.get("failure")
    if payload.get("status") == "SUCCEEDED":
        return failure is None
    if not isinstance(failure, Mapping) or set(failure) != {"code", "stage"}:
        return False
    return bool(
        failure.get("code")
        in {
            "invalid_input",
            "preflight_failed",
            "probe_failed",
            "encoder_unavailable",
            "encode_failed",
            "reframe_failed",
            "chroma_failed",
            "batch_failed",
            "audio_failed",
            "output_invalid",
            "cancelled_by_user",
            "paused_by_user",
            "shutdown",
            "internal_error",
            "unknown",
        }
        and failure.get("stage")
        in {
            "preflight",
            "probe",
            "reframe",
            "batch",
            "chroma",
            "audio",
            "encode",
            "output_validation",
            "lifecycle",
            "unknown",
        }
    )


def _valid_v5_hardware_profile(profile: Any) -> bool:
    if not isinstance(profile, Mapping) or set(profile) != {
        "profile_version",
        "os_family",
        "os_version",
        "architecture",
        "cpu_model",
        "logical_cpu_count",
        "ram_total_mb",
        "gpu_adapters",
        "encoder_capabilities",
    }:
        return False
    if profile.get("profile_version") != 1:
        return False
    if profile.get("os_family") not in {"windows", "macos", "linux", "other"}:
        return False
    version = profile.get("os_version")
    if version is not None and (
        not isinstance(version, str)
        or len(version) > 16
        or not _COARSE_OS_VERSION_RE.fullmatch(version)
    ):
        return False
    if profile.get("architecture") not in {
        "x86_64",
        "x86",
        "arm64",
        "arm",
        "other",
    }:
        return False
    cpu_model = profile.get("cpu_model")
    if cpu_model is not None:
        if not isinstance(cpu_model, str) or len(cpu_model) > 64:
            return False
        if cpu_model not in _COARSE_CPU_MODELS and not re.fullmatch(
            r"Apple M[0-9]{1,2}(?: (?:Pro|Max|Ultra))?", cpu_model
        ):
            return False
    if not _strict_optional_int(
        profile.get("logical_cpu_count"), 1, 4096
    ) or not _strict_optional_int(
        profile.get("ram_total_mb"), 256, 4_194_304
    ):
        return False

    adapters = profile.get("gpu_adapters")
    if not isinstance(adapters, list) or len(adapters) > 8:
        return False
    for adapter in adapters:
        if not isinstance(adapter, Mapping) or set(adapter) != {
            "vendor",
            "model",
            "memory_mb",
        }:
            return False
        if adapter.get("vendor") not in {
            "nvidia",
            "amd",
            "intel",
            "apple",
            "other",
        }:
            return False
        model = adapter.get("model")
        if (
            not isinstance(model, str)
            or not _SAFE_GPU_MODEL_RE.fullmatch(model)
            or _RAW_IDENTIFIER_RE.search(model)
        ):
            return False
        if not _strict_optional_int(adapter.get("memory_mb"), 256, 1_048_576):
            return False
    adapter_keys = [
        (item["vendor"], item["model"], item["memory_mb"] or 0)
        for item in adapters
    ]
    if adapter_keys != sorted(adapter_keys) or len(adapter_keys) != len(
        set(adapter_keys)
    ):
        return False

    capabilities = profile.get("encoder_capabilities")
    encoder_allowlist = _ENCODERS - {"auto"}
    if not isinstance(capabilities, list) or len(capabilities) > 8:
        return False
    if not all(
        isinstance(item, str) and item in encoder_allowlist
        for item in capabilities
    ):
        return False
    return capabilities == sorted(set(capabilities))


def _valid_v5_finish_details(payload: Mapping[str, Any]) -> bool:
    stage_metrics = payload.get("stage_metrics")
    if not isinstance(stage_metrics, list) or len(stage_metrics) > 16:
        return False
    stage_names: list[str] = []
    for row in stage_metrics:
        if not isinstance(row, Mapping) or set(row) != {
            "stage",
            "status",
            "expected",
            "succeeded",
            "failed",
            "skipped",
            "cancelled",
            "duration_ms",
        }:
            return False
        if row.get("stage") not in _STAGE_IDS or row.get("status") not in _STAGE_STATUSES:
            return False
        stage_names.append(str(row["stage"]))
        for key in (
            "expected",
            "succeeded",
            "failed",
            "skipped",
            "cancelled",
        ):
            if not _is_nonnegative_int(row.get(key)) or row[key] > 100_000_000:
                return False
        if (
            row["succeeded"]
            + row["failed"]
            + row["skipped"]
            + row["cancelled"]
            > row["expected"]
        ):
            return False
        if not _strict_optional_int(
            row.get("duration_ms"), 0, 31_536_000_000
        ):
            return False
    if stage_names != sorted(set(stage_names), key=_STAGE_ORDER.index):
        return False

    encoder_metrics = payload.get("encoder_metrics")
    if not isinstance(encoder_metrics, list) or len(encoder_metrics) > 16:
        return False
    encoder_names: list[str] = []
    for row in encoder_metrics:
        if not isinstance(row, Mapping) or set(row) != {
            "encoder",
            "attempts",
            "successes",
            "failures",
            "wall_ms",
            "encoded_media_ms",
            "speed_x_avg",
        }:
            return False
        encoder = row.get("encoder")
        if encoder not in _ENCODERS - {"auto"}:
            return False
        encoder_names.append(str(encoder))
        for key in ("attempts", "successes", "failures"):
            if not _is_nonnegative_int(row.get(key)) or row[key] > 100_000_000:
                return False
        if row["successes"] + row["failures"] != row["attempts"]:
            return False
        for key in ("wall_ms", "encoded_media_ms"):
            if not _is_nonnegative_int(row.get(key)) or row[key] > 31_536_000_000:
                return False
        if not _strict_optional_number(row.get("speed_x_avg"), 0.0, 10_000.0):
            return False
    return encoder_names == sorted(set(encoder_names))


def _valid_v6_resource_samples(payload: Mapping[str, Any]) -> bool:
    samples = payload.get("resource_samples")
    if not isinstance(samples, list) or len(samples) > 120:
        return False
    duration_ms = payload.get("duration_ms")
    if not _is_nonnegative_int(duration_ms) or duration_ms > 31_536_000_000:
        return False
    offsets: list[int] = []
    for sample in samples:
        if not isinstance(sample, Mapping) or set(sample) != {
            "offset_ms",
            "cpu_percent",
            "ram_mb",
            "gpu_percent",
            "gpu_memory_mb",
        }:
            return False
        offset_ms = sample.get("offset_ms")
        if (
            not _is_nonnegative_int(offset_ms)
            or offset_ms > 31_536_000_000
            or offset_ms > duration_ms
        ):
            return False
        offsets.append(offset_ms)
        values = (
            sample.get("cpu_percent"),
            sample.get("ram_mb"),
            sample.get("gpu_percent"),
            sample.get("gpu_memory_mb"),
        )
        if all(value is None for value in values):
            return False
        if not _strict_optional_number(values[0], 0.0, 100.0):
            return False
        if not _strict_optional_number(values[1], 0.0, 4_194_304.0):
            return False
        if not _strict_optional_number(values[2], 0.0, 100.0):
            return False
        if not _strict_optional_number(values[3], 0.0, 1_048_576.0):
            return False
    return offsets == sorted(set(offsets))


def _valid_started_event(payload: Mapping[str, Any]) -> bool:
    schema_version = payload.get("schema_version")
    if schema_version in {
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    }:
        expected_fields = {
            "schema_version",
            "event_id",
            "client_run_id",
            "event_type",
            "occurred_at",
            "app_version",
            "tc_id",
            "input_count",
            "profile",
        } | _identity_fields(payload)
        if schema_version in {
            _HARDWARE_SCHEMA_VERSION,
            _RESOURCE_SCHEMA_VERSION,
        }:
            expected_fields.add("hardware_profile")
        return bool(
            set(payload) == expected_fields
            and _valid_common_lifecycle(payload, _RUN_STARTED)
            and _valid_identity_payload(payload)
            and _valid_v4_profile(payload.get("profile"))
            and (
                schema_version
                not in {_HARDWARE_SCHEMA_VERSION, _RESOURCE_SCHEMA_VERSION}
                or _valid_v5_hardware_profile(payload.get("hardware_profile"))
            )
        )
    expected_fields = {
        "schema_version",
        "event_id",
        "client_run_id",
        "event_type",
        "occurred_at",
        "app_version",
        "tc_id",
        "input_count",
        "encoder",
        "width",
        "height",
        "fps",
        "parallel_workers",
        "cpu_percent",
    } | _identity_fields(payload)
    if set(payload) != expected_fields:
        return False
    if not _valid_common_lifecycle(
        payload, _RUN_STARTED
    ) or not _valid_identity_payload(payload):
        return False
    encoder = payload.get("encoder")
    if encoder is not None and encoder not in _ENCODERS:
        return False
    ranges = {
        "width": (1, 16_384),
        "height": (1, 16_384),
        "fps": (1, 240),
        "parallel_workers": (1, 256),
        "cpu_percent": (0, 100),
    }
    return all(
        payload.get(key) is None
        or (
            _is_nonnegative_int(payload.get(key))
            and low <= payload[key] <= high
        )
        for key, (low, high) in ranges.items()
    )


def _valid_finished_event(payload: Mapping[str, Any]) -> bool:
    expected_fields = {
        "schema_version",
        "event_id",
        "client_run_id",
        "event_type",
        "occurred_at",
        "app_version",
        "tc_id",
        "input_count",
        "output_count",
        "expected_output_count",
        "duration_ms",
        "status",
    }
    schema_version = payload.get("schema_version")
    if schema_version in {
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    }:
        expected_fields |= {"media", "resources", "encoding", "failure"}
    if schema_version in {
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    }:
        expected_fields |= {"stage_metrics", "encoder_metrics"}
    if schema_version == _RESOURCE_SCHEMA_VERSION:
        expected_fields.add("resource_samples")
    expected_fields |= _identity_fields(payload)
    if set(payload) != expected_fields:
        return False
    if not _valid_common_lifecycle(
        payload, _RUN_FINISHED
    ) or not _valid_identity_payload(payload):
        return False
    expected = payload.get("expected_output_count")
    valid_common = bool(
        _is_nonnegative_int(payload.get("output_count"))
        and payload["output_count"] <= 100_000_000
        and (
            expected is None
            or (_is_nonnegative_int(expected) and expected <= 100_000_000)
        )
        and _is_nonnegative_int(payload.get("duration_ms"))
        and payload["duration_ms"] <= 31_536_000_000
        and payload.get("status") in TERMINAL_STATUSES
    )
    if not valid_common:
        return False
    if schema_version in {
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    }:
        if not _valid_v4_metrics(payload):
            return False
    if schema_version in {
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    } and not _valid_v5_finish_details(payload):
        return False
    return bool(
        schema_version != _RESOURCE_SCHEMA_VERSION
        or _valid_v6_resource_samples(payload)
    )


def _valid_legacy_event(payload: Mapping[str, Any]) -> bool:
    if set(payload) != {
        "schema_version",
        "event_id",
        "occurred_at",
        "app_version",
        "tc_id",
        "input_count",
        "output_count",
        "duration_sec",
        "status",
    }:
        return False
    if payload.get("schema_version") != _LEGACY_SCHEMA_VERSION:
        return False
    if payload.get("tc_id") not in LEGACY_SUPPORTED_TCS:
        return False
    if _normalize_app_version(payload.get("app_version")) != payload.get("app_version"):
        return False
    if payload.get("status") not in TERMINAL_STATUSES:
        return False
    if not _EVENT_ID_RE.fullmatch(str(payload.get("event_id", ""))):
        return False
    if not _valid_timestamp(payload.get("occurred_at")):
        return False
    if not all(
        _is_nonnegative_int(payload.get(key))
        for key in ("input_count", "output_count")
    ):
        return False
    duration = payload.get("duration_sec")
    return bool(
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(float(duration))
        and float(duration) >= 0.0
    )


def _valid_event_payload(payload: Mapping[str, Any]) -> bool:
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return False
    if schema_version == _LEGACY_SCHEMA_VERSION:
        return _valid_legacy_event(payload)
    if schema_version not in {
        _LIFECYCLE_SCHEMA_VERSION,
        _IDENTITY_SCHEMA_VERSION,
        _METRICS_SCHEMA_VERSION,
        _HARDWARE_SCHEMA_VERSION,
        _RESOURCE_SCHEMA_VERSION,
    }:
        return False
    event_type = payload.get("event_type")
    if event_type == _RUN_STARTED:
        return _valid_started_event(payload)
    if event_type == _RUN_FINISHED:
        return _valid_finished_event(payload)
    return False


def new_client_run_id() -> str:
    """Return a path-free stable id shared by exactly one start/finish pair."""

    return uuid.uuid4().hex


def _identity_from_record(record: Mapping[str, Any]) -> Optional[StatsIdentity]:
    """Return one canonical optional identity without gating telemetry."""

    candidate = record.get("identity")
    values = candidate.as_payload() if isinstance(candidate, StatsIdentity) else (
        candidate if isinstance(candidate, Mapping) else record
    )
    try:
        return normalize_identity(
            values.get("identity_type"), values.get("identity_value")
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _primary_input_count(
    tc_id: str,
    inputs: Any,
    preflight_inputs: Any = None,
) -> int:
    groups = inputs if isinstance(inputs, Mapping) else {}
    preflight = preflight_inputs if isinstance(preflight_inputs, Mapping) else {}
    if tc_id in {"TC01", "TC02", "TC03", "TC04"}:
        return _sequence_count(groups.get("product"))
    if tc_id == "TC05":
        return _sequence_count(groups.get("source"))
    if tc_id == "TC06":
        # A selected parent root can contain many green-screen source files.
        # The already-completed preflight snapshot is therefore the truthful
        # count; the selected root count is only a fail-safe fallback.
        resolved_products = _sequence_count(preflight.get("product"))
        return resolved_products or _sequence_count(groups.get("product_root"))
    return 0


def _profile_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    settings = record.get("settings")
    panels = settings if isinstance(settings, Mapping) else {}

    def first(*keys: str) -> Any:
        for preferred in ("video", "reframe", "batch", "audio_master"):
            panel = panels.get(preferred)
            if not isinstance(panel, Mapping):
                continue
            for key in keys:
                if key in panel:
                    return panel.get(key)
        for panel in panels.values():
            if not isinstance(panel, Mapping):
                continue
            for key in keys:
                if key in panel:
                    return panel.get(key)
        return None

    preflight = record.get("preflight")
    selected_encoder = (
        preflight.get("selected_encoder")
        if isinstance(preflight, Mapping)
        else None
    )
    encoder = str(selected_encoder or first("encoder") or "").strip().casefold()
    if encoder not in _ENCODERS:
        encoder = None
    return {
        "encoder": encoder,
        "width": _bounded_int_or_none(first("width"), 1, 16_384),
        "height": _bounded_int_or_none(first("height"), 1, 16_384),
        "fps": _bounded_int_or_none(first("fps"), 1, 240),
        "parallel_workers": _bounded_int_or_none(
            first("parallel_workers", "max_parallel", "ffmpeg_workers"),
            1,
            256,
        ),
        # Sampling CPU usage on the Tk thread would either block or add a
        # platform dependency.  The field is reserved and deliberately null.
        "cpu_percent": None,
    }


def _safe_metrics_snapshot(snapshot: Any, *, status: str) -> Optional[Mapping[str, Any]]:
    """Validate local metrics while accepting the pre-P1/P2 local shapes."""

    try:
        from .run_metrics import (
            is_valid_metrics_snapshot,
            unavailable_metrics_snapshot,
        )

        safe = snapshot
        if isinstance(safe, Mapping) and safe.get("metrics_version") in {1, 2}:
            safe = dict(safe)
            safe["metrics_version"] = 3
            safe.setdefault("stage_metrics", [])
            safe.setdefault("encoder_metrics", [])
            safe.setdefault("resource_samples", [])
        if not is_valid_metrics_snapshot(safe):
            safe = unavailable_metrics_snapshot(status=status)
        if not is_valid_metrics_snapshot(safe):
            return None
    except Exception:
        return None
    return safe


def _v4_metrics_from_snapshot(
    snapshot: Any, *, status: str
) -> Optional[dict[str, Any]]:
    """Map the local rich collector into the server's minimal v4 allow-list."""

    safe = _safe_metrics_snapshot(snapshot, status=status)
    if safe is None:
        return None

    media_source = safe["media"]
    input_duration_ms = (
        media_source.get("primary_duration_ms_total")
        if media_source.get("primary_duration_count", 0) > 0
        else None
    )
    produced_duration_ms = media_source.get("produced_duration_ms")

    resource_source = safe["resources"]
    sample_count = int(resource_source.get("sample_count", 0) or 0)
    cpu_avg = resource_source.get("ffmpeg_cpu_avg_pct")
    cpu_peak = resource_source.get("ffmpeg_cpu_peak_pct")
    rss_avg = resource_source.get("ffmpeg_rss_avg_bytes")
    rss_peak = resource_source.get("ffmpeg_rss_peak_bytes")
    if sample_count <= 0 or any(
        value is None for value in (cpu_avg, cpu_peak, rss_avg, rss_peak)
    ):
        sample_count = 0
        cpu_avg = cpu_peak = rss_avg = rss_peak = None
    resources = {
        "sample_count": sample_count,
        "cpu_avg_percent": round(float(cpu_avg), 3) if cpu_avg is not None else None,
        "cpu_peak_percent": round(float(cpu_peak), 3) if cpu_peak is not None else None,
        "ram_avg_mb": (
            round(float(rss_avg) / (1024.0 * 1024.0), 3)
            if rss_avg is not None
            else None
        ),
        "ram_peak_mb": (
            round(float(rss_peak) / (1024.0 * 1024.0), 3)
            if rss_peak is not None
            else None
        ),
    }

    encoding_source = safe["encoding"]
    actual_encoder = encoding_source.get("actual_encoder")
    if actual_encoder == "MIXED":
        actual_encoder = "mixed"
    if actual_encoder not in (_ENCODERS - {"auto"}) | {"mixed", None}:
        actual_encoder = None
    encoding = {
        "actual_encoder": actual_encoder,
        "fallback_used": bool(
            encoding_source.get("cpu_fallback_trigger_count", 0) > 0
        ),
    }
    if encoding["fallback_used"] and encoding["actual_encoder"] is None:
        encoding["fallback_used"] = False

    failure = None
    if status != "SUCCEEDED":
        failure_source = safe["failure"]
        failure_code = {
            "USER_CANCELLED": "cancelled_by_user",
            "USER_PAUSED": "paused_by_user",
            "APP_SHUTDOWN": "shutdown",
            "INVALID_INPUT": "invalid_input",
            "WATCHDOG_IDLE": "encode_failed",
            "WATCHDOG_WALL": "encode_failed",
            "ENCODER_PROCESS_FAILED": "encode_failed",
            "CPU_FALLBACK_EXHAUSTED": "encoder_unavailable",
            "OUTPUT_VALIDATION_FAILED": "output_invalid",
            "PIPELINE_INVARIANT": "internal_error",
            "UNKNOWN": "unknown",
        }.get(str(failure_source.get("code", "")).upper(), "unknown")
        failure_stage = {
            "REFRAME": "reframe",
            "CHROMA": "chroma",
            "BATCH_CHROMA": "batch",
            "AUDIO_MASTER": "audio",
        }.get(str(failure_source.get("stage", "")).upper(), "unknown")
        if failure_code == "output_invalid":
            failure_stage = "output_validation"
        elif failure_code in {"cancelled_by_user", "paused_by_user", "shutdown"}:
            failure_stage = "lifecycle"
        elif failure_code in {"encode_failed", "encoder_unavailable"}:
            failure_stage = "encode"
        failure = {"code": failure_code, "stage": failure_stage}

    mapped = {
        "media": {
            "input_duration_ms": input_duration_ms,
            "produced_duration_ms": produced_duration_ms,
        },
        "resources": resources,
        "encoding": encoding,
        "failure": failure,
    }
    probe = {"status": status, **mapped}
    return mapped if _valid_v4_metrics(probe) else None


def _v5_hardware_profile(value: Any) -> Optional[dict[str, Any]]:
    """Return an exact, coarse hardware allowlist with an unavailable fallback."""

    try:
        from .hardware_profile import (
            is_valid_hardware_profile,
            sanitize_hardware_profile,
            unavailable_hardware_profile,
        )

        safe = sanitize_hardware_profile(value)
        if not is_valid_hardware_profile(safe):
            safe = unavailable_hardware_profile()
        if not is_valid_hardware_profile(safe):
            return None
    except Exception:
        return None
    mapped = {
        "profile_version": safe["profile_version"],
        "os_family": safe["os_family"],
        "os_version": safe["os_version"],
        "architecture": safe["architecture"],
        "cpu_model": safe["cpu_model"],
        "logical_cpu_count": safe["logical_cpu_count"],
        "ram_total_mb": safe["ram_total_mb"],
        "gpu_adapters": [
            {
                "vendor": item["vendor"],
                "model": item["model"],
                "memory_mb": item["memory_mb"],
            }
            for item in safe["gpu_adapters"]
        ],
        "encoder_capabilities": list(safe["encoder_capabilities"]),
    }
    return mapped if _valid_v5_hardware_profile(mapped) else None


def _v5_finish_details_from_snapshot(
    snapshot: Any, *, status: str
) -> Optional[dict[str, Any]]:
    """Map local PipelineResult/encoder aggregates to the strict v5 rows."""

    safe = _safe_metrics_snapshot(snapshot, status=status)
    if safe is None:
        return None
    stage_metrics = [
        {
            "stage": row["stage"],
            "status": row["status"],
            "expected": row["expected"],
            "succeeded": row["succeeded"],
            "failed": row["failed"],
            "skipped": row["skipped"],
            "cancelled": row["cancelled"],
            "duration_ms": row["duration_ms"],
        }
        for row in safe["stage_metrics"]
    ]
    encoder_metrics = [
        {
            "encoder": row["encoder"],
            "attempts": row["attempts"],
            "successes": row["successes"],
            "failures": row["failures"],
            "wall_ms": row["wall_ms"],
            "encoded_media_ms": row["encoded_media_ms"],
            "speed_x_avg": row["speed_x_avg"],
        }
        for row in safe["encoder_metrics"]
    ]
    mapped = {
        "stage_metrics": stage_metrics,
        "encoder_metrics": encoder_metrics,
    }
    return mapped if _valid_v5_finish_details(mapped) else None


def _v6_resource_samples_from_snapshot(
    snapshot: Any,
    *,
    status: str,
    duration_ms: int,
) -> Optional[list[dict[str, Any]]]:
    """Map local v3 samples, clipping only timing to the accepted run duration."""

    safe = _safe_metrics_snapshot(snapshot, status=status)
    if safe is None:
        return None
    samples: list[dict[str, Any]] = []
    for source in safe["resource_samples"]:
        sample = {
            "offset_ms": min(int(source["offset_ms"]), duration_ms),
            "cpu_percent": source["cpu_percent"],
            "ram_mb": source["ram_mb"],
            "gpu_percent": source["gpu_percent"],
            "gpu_memory_mb": source["gpu_memory_mb"],
        }
        if samples and samples[-1]["offset_ms"] == sample["offset_ms"]:
            # Several late local samples can collapse onto the terminal edge;
            # retaining the newest keeps first/latest semantics and uniqueness.
            samples[-1] = sample
        else:
            samples.append(sample)
    probe = {"duration_ms": duration_ms, "resource_samples": samples}
    return samples if _valid_v6_resource_samples(probe) else None


def build_started_event(record: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build an exact v2-v6 start event without high-cardinality values."""

    tc_id = str(record.get("tc_id", record.get("label", "")) or "").strip().upper()
    client_run_id = str(
        record.get("client_run_id", record.get("id", "")) or ""
    ).strip().casefold()
    if tc_id not in SUPPORTED_TCS or not _CLIENT_RUN_ID_RE.fullmatch(client_run_id):
        return None
    explicit_count = record.get("input_count")
    input_count = (
        explicit_count
        if _is_nonnegative_int(explicit_count)
        else _primary_input_count(
            tc_id,
            record.get("inputs"),
            record.get("preflight_inputs"),
        )
    )
    profile = _profile_from_record(record)
    identity_snapshot = _identity_from_record(record)
    identity = (
        (identity_snapshot.identity_type, identity_snapshot.identity_value)
        if identity_snapshot is not None
        else None
    )
    requested_v6 = record.get("schema_version") == _RESOURCE_SCHEMA_VERSION
    requested_v5 = record.get("schema_version") == _HARDWARE_SCHEMA_VERSION
    requested_v4 = record.get("schema_version") == _METRICS_SCHEMA_VERSION
    payload = {
        "schema_version": (
            _RESOURCE_SCHEMA_VERSION
            if requested_v6
            else (
                _HARDWARE_SCHEMA_VERSION
                if requested_v5
                else (
                    _METRICS_SCHEMA_VERSION
                    if requested_v4
                    else (
                        _IDENTITY_SCHEMA_VERSION
                        if identity
                        else _LIFECYCLE_SCHEMA_VERSION
                    )
                )
            )
        ),
        "event_id": f"{client_run_id}-start",
        "client_run_id": client_run_id,
        "event_type": _RUN_STARTED,
        "occurred_at": _utc_timestamp(record.get("occurred_at")),
        "app_version": _app_version_from_record(record),
        "tc_id": tc_id,
        "input_count": input_count,
    }
    if requested_v4 or requested_v5 or requested_v6:
        payload["profile"] = profile
    else:
        payload.update(profile)
    if requested_v5 or requested_v6:
        hardware_profile = _v5_hardware_profile(record.get("hardware_profile"))
        if hardware_profile is None:
            return None
        payload["hardware_profile"] = hardware_profile
    if identity:
        payload.update(identity_type=identity[0], identity_value=identity[1])
    return payload if _valid_started_event(payload) else None


def build_finished_event(record: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Build an exact v2-v6 finish event without paths or raw errors."""

    tc_id = str(record.get("tc_id", record.get("label", "")) or "").strip().upper()
    client_run_id = str(
        record.get("client_run_id", record.get("id", "")) or ""
    ).strip().casefold()
    if tc_id not in SUPPORTED_TCS or not _CLIENT_RUN_ID_RE.fullmatch(client_run_id):
        return None

    explicit_count = record.get("input_count")
    input_count = (
        explicit_count
        if _is_nonnegative_int(explicit_count)
        else _primary_input_count(
            tc_id,
            record.get("inputs"),
            record.get("preflight_inputs"),
        )
    )
    pipeline = record.get("pipeline_result")
    pipeline_map = pipeline if isinstance(pipeline, Mapping) else {}
    if pipeline_map:
        tc04_counts = (
            _tc04_final_stage_counts(pipeline_map) if tc_id == "TC04" else None
        )
        if tc04_counts is not None:
            output_count, expected_output_count = tc04_counts
        else:
            # ``valid_output_count`` includes validated checkpoint outputs.
            # Public totals count only files produced in this attempt.
            produced = pipeline_map.get("produced_this_run")
            if not _is_nonnegative_int(produced):
                produced = pipeline_map.get("succeeded")
            output_count = produced if _is_nonnegative_int(produced) else 0
            expected_raw = pipeline_map.get("expected")
            expected_output_count = (
                expected_raw if _is_nonnegative_int(expected_raw) else None
            )
    else:
        output_count = _sequence_count(record.get("outputs"))
        expected_output_count = None
    explicit_duration_ms = record.get("duration_ms")
    if _is_nonnegative_int(explicit_duration_ms):
        duration_ms = min(explicit_duration_ms, 31_536_000_000)
    else:
        seconds = _bounded_float(
            record.get("elapsed_sec", 0.0), 0.0, 0.0, 31_536_000.0
        )
        duration_ms = int(round(seconds * 1000.0))
    identity_snapshot = _identity_from_record(record)
    identity = (
        (identity_snapshot.identity_type, identity_snapshot.identity_value)
        if identity_snapshot is not None
        else None
    )
    requested_v6 = record.get("schema_version") == _RESOURCE_SCHEMA_VERSION
    requested_v5 = record.get("schema_version") == _HARDWARE_SCHEMA_VERSION
    requested_v4 = record.get("schema_version") == _METRICS_SCHEMA_VERSION
    terminal_status = _status_from_record(record)
    payload = {
        "schema_version": (
            _RESOURCE_SCHEMA_VERSION
            if requested_v6
            else (
                _HARDWARE_SCHEMA_VERSION
                if requested_v5
                else (
                    _METRICS_SCHEMA_VERSION
                    if requested_v4
                    else (
                        _IDENTITY_SCHEMA_VERSION
                        if identity
                        else _LIFECYCLE_SCHEMA_VERSION
                    )
                )
            )
        ),
        "event_id": f"{client_run_id}-finish",
        "client_run_id": client_run_id,
        "event_type": _RUN_FINISHED,
        "occurred_at": _utc_timestamp(record.get("finished_at")),
        "app_version": _app_version_from_record(record),
        "tc_id": tc_id,
        "input_count": input_count,
        "output_count": output_count,
        "expected_output_count": expected_output_count,
        "duration_ms": duration_ms,
        "status": terminal_status,
    }
    if requested_v4 or requested_v5 or requested_v6:
        metrics = _v4_metrics_from_snapshot(
            record.get("metrics"), status=terminal_status
        )
        if metrics is None:
            return None
        payload.update(metrics)
    if requested_v5 or requested_v6:
        details = _v5_finish_details_from_snapshot(
            record.get("metrics"), status=terminal_status
        )
        if details is None:
            return None
        payload.update(details)
    if requested_v6:
        resource_samples = _v6_resource_samples_from_snapshot(
            record.get("metrics"),
            status=terminal_status,
            duration_ms=duration_ms,
        )
        if resource_samples is None:
            return None
        payload["resource_samples"] = resource_samples
    if identity:
        payload.update(identity_type=identity[0], identity_value=identity[1])
    return payload if _valid_finished_event(payload) else None


def build_event(record: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Create the complete allow-listed network payload from one history row.

    File paths, file names, settings, errors, machine/host identity, and media
    metadata are deliberately excluded.  Returning ``None`` means the record
    is outside the legacy TC01-TC04 contract or lacks an idempotency key.
    """

    tc_id = str(record.get("label", "") or "").strip().upper()
    event_id = str(record.get("id", "") or "").strip()
    if tc_id not in LEGACY_SUPPORTED_TCS or not _EVENT_ID_RE.fullmatch(event_id):
        return None
    inputs = record.get("inputs")
    product_inputs = inputs.get("product", []) if isinstance(inputs, Mapping) else []
    duration = _bounded_float(record.get("elapsed_sec", 0.0), 0.0, 0.0, 31_536_000.0)
    try:
        occurred_at = datetime.fromisoformat(
            str(record.get("created_at", "") or "")
        )
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
    except (TypeError, ValueError):
        occurred_at = datetime.now(timezone.utc)
    return {
        "schema_version": _LEGACY_SCHEMA_VERSION,
        "event_id": event_id,
        "occurred_at": occurred_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "app_version": _app_version_from_record(record),
        "tc_id": tc_id,
        "input_count": _sequence_count(product_inputs),
        "output_count": _sequence_count(record.get("outputs")),
        "duration_sec": round(duration, 3),
        "status": _status_from_record(record),
    }


class UsageStatsDispatcher:
    """Background SQLite outbox with idempotent, bounded HTTP retry."""

    def __init__(
        self,
        *,
        data_dir: Optional[Path] = None,
        config_loader: Optional[Callable[[], StatsConfig]] = None,
        opener: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
        app_version: str = _DEFAULT_APP_VERSION,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.db_path = self.data_dir / "usage_stats_outbox.sqlite3"
        self._bootstrap_enabled = config_loader is None
        self._config_loader = config_loader or (
            lambda: load_config(data_dir=self.data_dir)
        )
        # urllib.request.urlopen follows redirects and may copy Authorization or
        # a custom installation-token header to a different host. The default
        # transport therefore rejects every redirect. Tests may still inject a
        # deterministic opener, but production never follows one implicitly.
        self._opener = opener or _urlopen_without_redirect
        self._clock = clock
        normalized_version = str(app_version or "").strip().upper()
        self._app_version = (
            normalized_version
            if _APP_VERSION_RE.fullmatch(normalized_version)
            else _DEFAULT_APP_VERSION
        )
        self._enrollment_lock = threading.Lock()
        self._enrollment_attempts = 0
        self._next_enrollment_attempt_at = 0.0
        self._queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._writer_wake = threading.Event()
        self._sender_wake = threading.Event()
        self._start_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._next_maintenance_monotonic = 0.0
        self._writer_thread: Optional[threading.Thread] = None
        self._sender_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    def enqueue(self, event: Mapping[str, Any]) -> bool:
        """Return immediately after an in-memory handoff; never do network I/O."""

        try:
            payload = dict(event)
            # Re-validate the strict payload schema before it reaches disk.
            if not _valid_event_payload(payload):
                return False
            # Start the dedicated writer before handoff.  The writer never
            # performs HTTP, so events become durable even while a prior send
            # is blocked on its network timeout.
            self.start()
            self._queue.put(payload)
            self._writer_wake.set()
            return True
        except Exception:
            return False

    def start(self) -> None:
        try:
            with self._start_lock:
                if (
                    self._writer_thread is not None
                    and self._writer_thread.is_alive()
                    and self._sender_thread is not None
                    and self._sender_thread.is_alive()
                ):
                    return
                self._stopping.clear()
                self._writer_thread = threading.Thread(
                    target=self._run_writer,
                    name="greenpc-usage-stats-writer",
                    daemon=True,
                )
                self._sender_thread = threading.Thread(
                    target=self._run_sender,
                    name="greenpc-usage-stats-sender",
                    daemon=True,
                )
                self._writer_thread.start()
                self._sender_thread.start()
        except Exception:
            # Starting statistics must never affect the render completion path.
            return

    def set_app_version(self, app_version: str) -> None:
        normalized = str(app_version or "").strip().upper()
        if _APP_VERSION_RE.fullmatch(normalized):
            self._app_version = normalized

    def stop(self, timeout: float = 1.0) -> None:
        self._stopping.set()
        self._writer_wake.set()
        self._sender_wake.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in (self._writer_thread, self._sender_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _safe_config(self) -> StatsConfig:
        try:
            config = self._config_loader()
            return config if isinstance(config, StatsConfig) else StatsConfig()
        except Exception:
            return StatsConfig()

    def _now(self) -> float:
        try:
            value = float(self._clock())
            return value if math.isfinite(value) else time.time()
        except Exception:
            return time.time()

    def _ensure_bootstrap(self) -> None:
        if not self._bootstrap_enabled:
            return
        try:
            bootstrap_device_config(data_dir=self.data_dir)
        except Exception:
            return

    def _schedule_enrollment_retry(self, config: StatsConfig) -> None:
        attempts = self._enrollment_attempts
        delay = min(
            config.retry_max_sec,
            config.retry_initial_sec * (2 ** min(attempts, 10)),
        )
        self._enrollment_attempts = attempts + 1
        self._next_enrollment_attempt_at = self._now() + delay

    def _attempt_enrollment(self, config: StatsConfig) -> bool:
        if not config.can_enroll or self._now() < self._next_enrollment_attempt_at:
            return False
        if not self._enrollment_lock.acquire(blocking=False):
            return False
        response = None
        try:
            payload = {
                "protocol_version": _ENROLLMENT_PROTOCOL_VERSION,
                "client_installation_id": config.client_installation_id,
                "enrollment_secret": config.enrollment_secret,
                "platform": _desktop_platform(),
                "app_version": self._app_version,
            }
            request = urllib.request.Request(
                _enrollment_endpoint(config.endpoint),
                data=json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": config.client_installation_id,
                },
                method="POST",
            )
            response = self._opener(request, timeout=config.timeout_sec)
            raw_status = getattr(response, "status", None)
            if raw_status is None and hasattr(response, "getcode"):
                raw_status = response.getcode()
            if int(raw_status) not in {200, 201}:
                self._schedule_enrollment_retry(config)
                return False
            raw_body = response.read(4097) if hasattr(response, "read") else b""
            if not isinstance(raw_body, (bytes, bytearray)) or len(raw_body) > 4096:
                self._schedule_enrollment_retry(config)
                return False
            decoded = json.loads(bytes(raw_body).decode("utf-8"))
            if not isinstance(decoded, dict) or set(decoded) != {
                "protocol_version",
                "installation_id",
                "token",
            }:
                self._schedule_enrollment_retry(config)
                return False
            if type(decoded.get("protocol_version")) is not int or decoded.get(
                "protocol_version"
            ) != _ENROLLMENT_PROTOCOL_VERSION:
                self._schedule_enrollment_retry(config)
                return False
            saved = _persist_enrollment_token(
                config,
                token=str(decoded.get("token", "")),
                server_installation_id=str(decoded.get("installation_id", "")),
                data_dir=self.data_dir,
            )
            if not saved:
                self._schedule_enrollment_retry(config)
                return False
            self._enrollment_attempts = 0
            self._next_enrollment_attempt_at = 0.0
            self._sender_wake.set()
            return True
        except HTTPError:
            self._schedule_enrollment_retry(config)
            return False
        except Exception:
            self._schedule_enrollment_retry(config)
            return False
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass
            self._enrollment_lock.release()

    def _connect(self) -> sqlite3.Connection:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=1.0)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    payload_bytes INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quarantine (
                    event_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    quarantined_at REAL NOT NULL
                )
                """
            )
            # Migrate the original donor schema in place.  Existing rows receive
            # a retention timestamp at first open instead of being discarded.
            outbox_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(outbox)")
            }
            if "created_at" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE outbox ADD COLUMN created_at REAL NOT NULL DEFAULT 0"
                )
            if "payload_bytes" not in outbox_columns:
                connection.execute(
                    """
                    ALTER TABLE outbox
                    ADD COLUMN payload_bytes INTEGER NOT NULL DEFAULT 0
                    """
                )
            now = self._now()
            connection.execute(
                "UPDATE outbox SET created_at = ? WHERE created_at <= 0", (now,)
            )
            connection.execute(
                """
                UPDATE outbox
                SET payload_bytes = length(CAST(payload_json AS BLOB))
                WHERE payload_bytes <= 0
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_due
                ON outbox(next_attempt_at, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quarantine_age
                ON quarantine(quarantined_at)
                """
            )
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _increment_meta_counter(
        connection: sqlite3.Connection, key: str, increment: int
    ) -> None:
        if increment <= 0:
            return
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        current = connection.execute(
            "SELECT value FROM stats_meta WHERE key = ?", (key,)
        ).fetchone()
        value = (int(current[0]) if current else 0) + int(increment)
        connection.execute(
            "INSERT OR REPLACE INTO stats_meta(key, value) VALUES (?, ?)",
            (key, value),
        )

    def _apply_retention(
        self,
        connection: sqlite3.Connection,
        config: StatsConfig,
        now: float,
    ) -> None:
        """Bound local scalar telemetry by age, row count, and payload bytes."""

        max_age = max(1.0, float(config.max_event_age_sec))
        cutoff = now - max_age
        expired = connection.execute(
            "DELETE FROM outbox WHERE created_at > 0 AND created_at < ?", (cutoff,)
        ).rowcount

        max_events = max(1, int(config.max_outbox_events))
        max_bytes = max(1024, int(config.max_outbox_bytes))
        rows = connection.execute(
            """
            SELECT rowid, payload_bytes
            FROM outbox
            ORDER BY created_at ASC, rowid ASC
            """
        ).fetchall()
        total_bytes = sum(max(0, int(row[1])) for row in rows)
        remove_rowids: list[int] = []
        index = 0
        while (
            len(rows) - len(remove_rowids) > max_events
            or total_bytes > max_bytes
        ) and index < len(rows):
            rowid, payload_bytes = rows[index]
            remove_rowids.append(int(rowid))
            total_bytes -= max(0, int(payload_bytes))
            index += 1
        if remove_rowids:
            connection.executemany(
                "DELETE FROM outbox WHERE rowid = ?",
                ((rowid,) for rowid in remove_rowids),
            )
        dropped = max(0, int(expired)) + len(remove_rowids)
        self._increment_meta_counter(connection, "outbox_dropped", dropped)

        quarantine_cutoff = now - max(
            1.0, float(config.max_quarantine_age_sec)
        )
        expired_quarantine = connection.execute(
            "DELETE FROM quarantine WHERE quarantined_at < ?", (quarantine_cutoff,)
        ).rowcount
        max_quarantine = max(1, int(config.max_quarantine_events))
        quarantine_count_row = connection.execute(
            "SELECT COUNT(*) FROM quarantine"
        ).fetchone()
        quarantine_count = int(quarantine_count_row[0]) if quarantine_count_row else 0
        excess = max(0, quarantine_count - max_quarantine)
        if excess:
            connection.execute(
                """
                DELETE FROM quarantine
                WHERE event_id IN (
                    SELECT event_id FROM quarantine
                    ORDER BY quarantined_at ASC, event_id ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
        self._increment_meta_counter(
            connection,
            "quarantine_dropped",
            max(0, int(expired_quarantine)) + excess,
        )

    def _maintain_retention(self, config: Optional[StatsConfig] = None) -> None:
        selected = config if isinstance(config, StatsConfig) else self._safe_config()
        monotonic_now = time.monotonic()
        with self._maintenance_lock:
            if monotonic_now < self._next_maintenance_monotonic:
                return
            self._next_maintenance_monotonic = (
                monotonic_now + _MAINTENANCE_INTERVAL_SEC
            )
        try:
            with self._db_lock, closing(self._connect()) as connection, connection:
                self._apply_retention(connection, selected, self._now())
        except Exception:
            # Retention is maintenance only; it cannot affect a render verdict.
            return

    def _store(self, event: Mapping[str, Any]) -> None:
        payload = json.dumps(
            dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload_bytes = len(payload.encode("utf-8"))
        config = self._safe_config()
        now = self._now()
        with self._db_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    event_id, payload_json, created_at, payload_bytes
                ) VALUES (?, ?, ?, ?)
                """,
                (str(event["event_id"]), payload, now, payload_bytes),
            )
            self._apply_retention(connection, config, now)

    def _due_rows(self, now: float, limit: int = 20) -> list[tuple[str, str, int]]:
        with self._db_lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT event_id, payload_json, attempts
                FROM outbox
                WHERE next_attempt_at <= ?
                ORDER BY rowid
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
        return [(str(row[0]), str(row[1]), int(row[2])) for row in rows]

    def _delete(self, event_id: str) -> None:
        with self._db_lock, closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM outbox WHERE event_id = ?", (event_id,))

    def _reschedule(self, event_id: str, attempts: int, config: StatsConfig) -> None:
        next_attempt = attempts + 1
        delay = min(
            config.retry_max_sec,
            config.retry_initial_sec * (2 ** min(attempts, 10)),
        )
        with self._db_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE outbox SET attempts = ?, next_attempt_at = ? WHERE event_id = ?",
                (next_attempt, self._now() + delay, event_id),
            )

    def _quarantine(self, event_id: str, reason: str) -> None:
        safe_reason = reason if reason in {
            "http_redirect_rejected",
            "http_409_conflict",
            "http_4xx_permanent",
            "payload_schema_invalid",
        } else "http_4xx_permanent"
        with self._db_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO quarantine(event_id, reason, quarantined_at)
                VALUES (?, ?, ?)
                """,
                (event_id, safe_reason, self._now()),
            )
            connection.execute("DELETE FROM outbox WHERE event_id = ?", (event_id,))
            self._apply_retention(connection, self._safe_config(), self._now())

    @staticmethod
    def _classify_http_status(status: int) -> tuple[str, str]:
        if 200 <= status < 300:
            return _DELIVERED, ""
        if 300 <= status < 400:
            return _PERMANENT, "http_redirect_rejected"
        if status in {408, 429} or status >= 500:
            return _RETRY, ""
        if status == 409:
            return _PERMANENT, "http_409_conflict"
        if 400 <= status < 500:
            return _PERMANENT, "http_4xx_permanent"
        return _RETRY, ""

    def _send(self, payload_json: str, config: StatsConfig) -> tuple[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": str(json.loads(payload_json)["event_id"]),
        }
        if config.token:
            token_value = config.token
            if config.token_prefix:
                token_value = f"{config.token_prefix} {token_value}"
            headers[config.token_header] = token_value
        request = urllib.request.Request(
            config.endpoint,
            data=payload_json.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response = None
        try:
            response = self._opener(request, timeout=config.timeout_sec)
            raw_status = getattr(response, "status", None)
            if raw_status is None and hasattr(response, "getcode"):
                raw_status = response.getcode()
            status = int(raw_status)
            if hasattr(response, "read"):
                response.read(1024)
            return self._classify_http_status(status)
        except HTTPError as exc:
            return self._classify_http_status(int(exc.code))
        except Exception:
            return _RETRY, ""
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass

    def drain_once(self) -> int:
        """Attempt currently due events; intended for worker and focused tests."""

        self._ensure_bootstrap()
        config = self._safe_config()
        self._maintain_retention(config)
        if not config.can_send and config.can_enroll:
            self._attempt_enrollment(config)
            config = self._safe_config()
        if not config.can_send:
            return 0
        delivered = 0
        for event_id, payload_json, attempts in self._due_rows(self._now()):
            try:
                decoded = json.loads(payload_json)
            except Exception:
                decoded = None
            if not isinstance(decoded, Mapping) or not _valid_event_payload(decoded):
                self._quarantine(event_id, "payload_schema_invalid")
                continue
            disposition, reason = self._send(payload_json, config)
            if disposition == _DELIVERED:
                self._delete(event_id)
                delivered += 1
            elif disposition == _PERMANENT:
                self._quarantine(event_id, reason)
            else:
                self._reschedule(event_id, attempts, config)
        return delivered

    def pending_count(self) -> int:
        try:
            with self._db_lock, closing(self._connect()) as connection, connection:
                row = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def quarantine_count(self) -> int:
        try:
            with self._db_lock, closing(self._connect()) as connection, connection:
                row = connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def health_snapshot(self) -> dict[str, Any]:
        """Return privacy-safe local status without creating or changing the DB.

        This is intentionally separate from ``pending_count``: a status UI may
        poll it frequently and must not start threads, perform HTTP, create the
        data directory, migrate schema, or expose endpoint/token values.
        """

        config = self._safe_config()
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "state": "disabled" if not config.enabled else "local_only",
            "enabled": bool(config.enabled),
            "can_send": bool(config.can_send),
            "database_exists": False,
            "database_readable": True,
            "pending_events": 0,
            "pending_bytes": 0,
            "quarantine_events": 0,
            "oldest_pending_age_sec": 0.0,
            "dropped_events": 0,
            "dropped_quarantine_events": 0,
            "writer_alive": bool(
                self._writer_thread is not None and self._writer_thread.is_alive()
            ),
            "sender_alive": bool(
                self._sender_thread is not None and self._sender_thread.is_alive()
            ),
            "retention": {
                "max_events": max(1, int(config.max_outbox_events)),
                "max_bytes": max(1024, int(config.max_outbox_bytes)),
                "max_age_sec": max(1.0, float(config.max_event_age_sec)),
            },
        }
        if not self.db_path.is_file():
            if config.can_send:
                snapshot["state"] = "ready"
            return snapshot

        snapshot["database_exists"] = True
        try:
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            with self._db_lock, closing(
                sqlite3.connect(uri, uri=True, timeout=0.25)
            ) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "outbox" in tables:
                    columns = {
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(outbox)")
                    }
                    if "payload_bytes" in columns:
                        pending = connection.execute(
                            """
                            SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0)
                            FROM outbox
                            """
                        ).fetchone()
                    else:
                        pending = connection.execute(
                            """
                            SELECT COUNT(*),
                                   COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0)
                            FROM outbox
                            """
                        ).fetchone()
                    snapshot["pending_events"] = int(pending[0]) if pending else 0
                    snapshot["pending_bytes"] = int(pending[1]) if pending else 0
                    if "created_at" in columns:
                        oldest = connection.execute(
                            "SELECT MIN(created_at) FROM outbox WHERE created_at > 0"
                        ).fetchone()
                        oldest_at = float(oldest[0]) if oldest and oldest[0] else 0.0
                        if oldest_at > 0:
                            snapshot["oldest_pending_age_sec"] = round(
                                max(0.0, self._now() - oldest_at), 3
                            )
                if "quarantine" in tables:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM quarantine"
                    ).fetchone()
                    snapshot["quarantine_events"] = int(row[0]) if row else 0
                if "stats_meta" in tables:
                    counters = dict(
                        connection.execute(
                            "SELECT key, value FROM stats_meta WHERE key IN (?, ?)",
                            ("outbox_dropped", "quarantine_dropped"),
                        ).fetchall()
                    )
                    snapshot["dropped_events"] = int(
                        counters.get("outbox_dropped", 0)
                    )
                    snapshot["dropped_quarantine_events"] = int(
                        counters.get("quarantine_dropped", 0)
                    )
        except Exception:
            snapshot["database_readable"] = False
            snapshot["state"] = "unavailable"
            return snapshot

        if not config.enabled:
            snapshot["state"] = "disabled"
        elif not config.can_send:
            snapshot["state"] = "local_only"
        elif snapshot["quarantine_events"]:
            snapshot["state"] = "attention"
        elif snapshot["pending_events"]:
            snapshot["state"] = "backlog"
        else:
            snapshot["state"] = "ready"
        return snapshot

    def _run_writer(self) -> None:
        while not self._stopping.is_set() or not self._queue.empty():
            stored_any = False
            while True:
                try:
                    event = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._store(event)
                    stored_any = True
                    self._sender_wake.set()
                except Exception:
                    # Keep the event in memory for a later bounded retry.
                    self._queue.put(event)
                    break
            self._writer_wake.clear()
            if not stored_any:
                self._writer_wake.wait(0.1)

    def _run_sender(self) -> None:
        while not self._stopping.is_set():
            try:
                self.drain_once()
            except Exception:
                pass
            self._sender_wake.clear()
            self._sender_wake.wait(0.5)


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_DISPATCHER: Optional[UsageStatsDispatcher] = None


def _default_dispatcher() -> UsageStatsDispatcher:
    global _DEFAULT_DISPATCHER
    with _DEFAULT_LOCK:
        if _DEFAULT_DISPATCHER is None:
            _DEFAULT_DISPATCHER = UsageStatsDispatcher()
        return _DEFAULT_DISPATCHER


def enqueue_job_stats(record: Mapping[str, Any]) -> bool:
    """Legacy v1 terminal hook retained for older integrations and tests."""

    try:
        event = build_event(record)
        if event is None:
            return False
        return _default_dispatcher().enqueue(event)
    except Exception:
        return False


def enqueue_run_started(record: Mapping[str, Any]) -> bool:
    """Best-effort schema-v2/v3 start hook; safe on the Tk caller thread."""

    try:
        event = (
            dict(record)
            if _valid_started_event(record)
            else build_started_event(record)
        )
        if event is None:
            return False
        return _default_dispatcher().enqueue(event)
    except Exception:
        return False


def enqueue_run_finished(record: Mapping[str, Any]) -> bool:
    """Best-effort schema-v2/v3 finish hook; never changes render truth."""

    try:
        event = (
            dict(record)
            if _valid_finished_event(record)
            else build_finished_event(record)
        )
        if event is None:
            return False
        return _default_dispatcher().enqueue(event)
    except Exception:
        return False


def start_default_dispatcher(app_version: str = _DEFAULT_APP_VERSION) -> None:
    """Start enrollment/backlog delivery without blocking the Tk caller."""

    try:
        dispatcher = _default_dispatcher()
        dispatcher.set_app_version(app_version)
        dispatcher.start()
    except Exception:
        pass


def stop_default_dispatcher(timeout: float = 1.0) -> None:
    """Persist queued events during graceful exit without raising."""

    dispatcher = _DEFAULT_DISPATCHER
    if dispatcher is None:
        return
    try:
        dispatcher.stop(timeout=max(0.0, min(float(timeout), 2.0)))
    except Exception:
        pass


def usage_stats_health_snapshot() -> dict[str, Any]:
    """Read-only, privacy-safe status for the desktop status surface."""

    try:
        return _default_dispatcher().health_snapshot()
    except Exception:
        # Keep a stable scalar-only shape even if local configuration or the
        # SQLite file is damaged.  Never expose the underlying exception.
        return {
            "schema_version": 1,
            "state": "unavailable",
            "enabled": False,
            "can_send": False,
            "database_exists": False,
            "database_readable": False,
            "pending_events": 0,
            "pending_bytes": 0,
            "quarantine_events": 0,
            "oldest_pending_age_sec": 0.0,
            "dropped_events": 0,
            "dropped_quarantine_events": 0,
            "writer_alive": False,
            "sender_alive": False,
            "retention": {
                "max_events": _DEFAULT_MAX_OUTBOX_EVENTS,
                "max_bytes": _DEFAULT_MAX_OUTBOX_BYTES,
                "max_age_sec": float(_DEFAULT_MAX_EVENT_AGE_SEC),
            },
        }
