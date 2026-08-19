"""Coarse, privacy-safe hardware capabilities for GreenStats telemetry.

The collector intentionally uses only local platform/process APIs.  It never
reads environment variables, invokes a command, or emits a hostname, account,
serial, MAC address, device identifier, path, or raw hardware dump.  Every
field is normalized into a small bounded vocabulary before it can leave this
module.
"""
from __future__ import annotations

import math
import os
import platform
import re
import unicodedata
from typing import Any, Iterable, Mapping, Optional

try:  # Optional at source-runtime level; packaged builds pin psutil.
    import psutil as _DEFAULT_PSUTIL  # type: ignore
except Exception:  # pragma: no cover - exercised through dependency injection.
    _DEFAULT_PSUTIL = None


PROFILE_VERSION = 1
MAX_LOGICAL_CPUS = 4096
MAX_RAM_MB = 4_194_304
MAX_GPU_MEMORY_MB = 1_048_576
MAX_GPU_ADAPTERS = 8
MAX_ENCODER_CAPABILITIES = 8

OS_FAMILIES = frozenset({"windows", "macos", "linux", "other"})
ARCHITECTURES = frozenset({"x86_64", "x86", "arm64", "arm", "other"})
GPU_VENDORS = frozenset({"nvidia", "amd", "intel", "apple", "other"})
ENCODER_CAPABILITIES = frozenset(
    {
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

PROFILE_FIELDS = frozenset(
    {
        "profile_version",
        "os_family",
        "os_version",
        "architecture",
        "cpu_model",
        "logical_cpu_count",
        "ram_total_mb",
        "gpu_adapters",
        "encoder_capabilities",
    }
)
GPU_FIELDS = frozenset({"vendor", "model", "memory_mb"})

_VERSION_NUMBER_RE = re.compile(r"[0-9]+")
_LONG_IDENTIFIER_RE = re.compile(
    r"(?:[0-9a-f]{12,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_FORBIDDEN_MODEL_WORD_RE = re.compile(
    r"\b(?:serial|uuid|device[ _-]?id|mac[ _-]?address|hostname)\b",
    re.IGNORECASE,
)
_COARSE_CPU_MODEL_RE = re.compile(
    r"^(?:Apple Silicon|Apple M[0-9]{1,2}(?: (?:Pro|Max|Ultra))?|"
    r"Intel(?: Xeon| Core i[3579])?|AMD(?: EPYC| Ryzen [3579])?|ARM)$"
)
_SAFE_GPU_MODEL_RE = re.compile(r"^[A-Za-z0-9 ._+()-]{1,80}$")


def _strict_int_or_none(value: Any, low: int, high: int) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if low <= number <= high else None


def _call(module: Any, name: str, default: Any = "") -> Any:
    try:
        function = getattr(module, name)
        return function() if callable(function) else default
    except Exception:
        return default


def _os_family(platform_module: Any) -> str:
    system = str(_call(platform_module, "system", "") or "").strip().casefold()
    if system == "darwin":
        return "macos"
    if system in {"windows", "linux"}:
        return system
    return "other"


def _coarse_os_version(platform_module: Any, os_family: str) -> Optional[str]:
    raw: Any = ""
    try:
        if os_family == "macos":
            raw = platform_module.mac_ver()[0]
        elif os_family == "windows":
            release = str(platform_module.release() or "").strip()
            version = str(platform_module.version() or "").strip()
            raw = release or version
        elif os_family == "linux":
            raw = platform_module.release()
    except Exception:
        raw = ""
    numbers = _VERSION_NUMBER_RE.findall(str(raw or ""))[:2]
    return ".".join(numbers) if numbers else None


def _architecture(platform_module: Any) -> str:
    raw = str(_call(platform_module, "machine", "") or "").strip().casefold()
    aliases = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "x64": "x86_64",
        "i386": "x86",
        "i486": "x86",
        "i586": "x86",
        "i686": "x86",
        "x86": "x86",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv8": "arm64",
        "arm": "arm",
        "armv7l": "arm",
    }
    return aliases.get(raw, "other")


def _coarse_cpu_model(
    platform_module: Any, os_family: str, architecture: str
) -> Optional[str]:
    raw = unicodedata.normalize(
        "NFKC", str(_call(platform_module, "processor", "") or "")
    )
    compact = " ".join(raw.replace("\x00", " ").split())
    lowered = compact.casefold()
    if os_family == "macos" and architecture == "arm64":
        apple = re.search(
            r"\bapple\s+m[0-9]{1,2}(?:\s+(?:pro|max|ultra))?\b",
            compact,
            re.IGNORECASE,
        )
        return apple.group(0).title() if apple else "Apple Silicon"
    if "apple" in lowered:
        return "Apple Silicon"
    if "intel" in lowered:
        if "xeon" in lowered:
            return "Intel Xeon"
        core = re.search(r"\bcore(?:\(tm\))?\s+i[3579]\b", compact, re.IGNORECASE)
        return (
            f"Intel {core.group(0).replace('(TM)', '').replace('(tm)', '')}"
            if core
            else "Intel"
        )
    if "amd" in lowered or "ryzen" in lowered:
        if "epyc" in lowered:
            return "AMD EPYC"
        ryzen = re.search(r"\bryzen\s+[3579]\b", compact, re.IGNORECASE)
        return f"AMD {ryzen.group(0).title()}" if ryzen else "AMD"
    if "arm" in lowered or architecture in {"arm", "arm64"}:
        return "ARM"
    return None


def _logical_cpu_count(os_module: Any) -> Optional[int]:
    try:
        return _strict_int_or_none(os_module.cpu_count(), 1, MAX_LOGICAL_CPUS)
    except Exception:
        return None


def _ram_total_mb(psutil_module: Any) -> Optional[int]:
    if psutil_module is None:
        return None
    try:
        total_bytes = int(psutil_module.virtual_memory().total)
    except Exception:
        return None
    if total_bytes <= 0:
        return None
    raw_mb = total_bytes / (1024.0 * 1024.0)
    if not math.isfinite(raw_mb):
        return None
    # Coarsen to 256 MiB buckets so the value remains useful for capacity
    # analysis without becoming an exact machine fingerprint.
    rounded = max(256, int(round(raw_mb / 256.0) * 256))
    return min(MAX_RAM_MB, rounded)


def _encoder_capabilities(values: Any) -> list[str]:
    if isinstance(values, (str, bytes, Mapping)) or values is None:
        return []
    try:
        candidates: Iterable[Any] = values
    except TypeError:
        return []
    safe = {
        str(value or "").strip().casefold()
        for value in candidates
        if str(value or "").strip().casefold() in ENCODER_CAPABILITIES
    }
    return sorted(safe)[:MAX_ENCODER_CAPABILITIES]


def _gpu_vendor(value: Any) -> str:
    text = str(value or "").strip().casefold()
    aliases = {
        "nvidia corporation": "nvidia",
        "advanced micro devices": "amd",
        "ati": "amd",
        "intel corporation": "intel",
        "apple inc.": "apple",
    }
    text = aliases.get(text, text)
    return text if text in GPU_VENDORS else "other"


def _gpu_model(value: Any, vendor: str) -> str:
    fallback = {
        "nvidia": "NVIDIA GPU",
        "amd": "AMD GPU",
        "intel": "Intel GPU",
        "apple": "Apple integrated GPU",
        "other": "Other GPU",
    }[vendor]
    raw = unicodedata.normalize("NFKC", str(value or "")).replace("\x00", " ")
    compact = " ".join(raw.split())[:80]
    if (
        not compact
        or _LONG_IDENTIFIER_RE.search(compact)
        or _FORBIDDEN_MODEL_WORD_RE.search(compact)
    ):
        return fallback
    patterns = {
        "nvidia": re.compile(
            r"\b(?:NVIDIA\s+)?(?:GeForce\s+)?(?:RTX|GTX|T)\s*[0-9]{3,4}"
            r"(?:\s+(?:Ti|SUPER))?\b",
            re.IGNORECASE,
        ),
        "amd": re.compile(
            r"\b(?:AMD\s+)?(?:Radeon\s+)?(?:RX|Pro)\s*[A-Za-z0-9]{2,8}\b",
            re.IGNORECASE,
        ),
        "intel": re.compile(
            r"\b(?:Intel\s+)?(?:Arc\s+[A-Za-z][0-9]{3}|Iris(?:\s+Xe)?|UHD)\b",
            re.IGNORECASE,
        ),
        "apple": re.compile(
            r"\bApple\s+M[0-9]{1,2}(?:\s+(?:Pro|Max|Ultra))?\b",
            re.IGNORECASE,
        ),
    }
    match = patterns.get(vendor, re.compile(r"$^")).search(compact)
    return " ".join(match.group(0).split())[:80] if match else fallback


def _sanitize_gpu_adapters(values: Any) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes, Mapping)) or values is None:
        return []
    try:
        candidates = list(values)
    except (TypeError, ValueError):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, Optional[int]]] = set()
    for raw in candidates[:MAX_GPU_ADAPTERS * 2]:
        if not isinstance(raw, Mapping):
            continue
        vendor = _gpu_vendor(raw.get("vendor"))
        model = _gpu_model(raw.get("model"), vendor)
        memory = _strict_int_or_none(
            raw.get("memory_mb"), 1, MAX_GPU_MEMORY_MB
        )
        if memory is not None:
            memory = max(256, int(round(memory / 256.0) * 256))
        key = (vendor, model.casefold(), memory)
        if key in seen:
            continue
        seen.add(key)
        result.append({"vendor": vendor, "model": model, "memory_mb": memory})
        if len(result) >= MAX_GPU_ADAPTERS:
            break
    return sorted(
        result,
        key=lambda item: (
            item["vendor"],
            item["model"],
            item["memory_mb"] or 0,
        ),
    )


def _derived_gpu_adapters(
    capabilities: Iterable[str], cpu_model: Optional[str] = None
) -> list[dict[str, Any]]:
    families: list[tuple[str, str]] = []
    caps = set(capabilities)
    if caps & {"h264_videotoolbox", "hevc_videotoolbox"}:
        apple_model = (
            cpu_model
            if cpu_model and re.fullmatch(r"Apple M[0-9]{1,2}(?: (?:Pro|Max|Ultra))?", cpu_model)
            else "Apple integrated GPU"
        )
        families.append(("apple", apple_model))
    if caps & {"h264_nvenc", "hevc_nvenc", "av1_nvenc"}:
        families.append(("nvidia", "NVIDIA GPU"))
    if "h264_qsv" in caps:
        families.append(("intel", "Intel GPU"))
    if "h264_amf" in caps:
        families.append(("amd", "AMD GPU"))
    return sorted(
        [
            {"vendor": vendor, "model": model, "memory_mb": None}
            for vendor, model in families[:MAX_GPU_ADAPTERS]
        ],
        key=lambda item: (item["vendor"], item["model"], 0),
    )


def unavailable_hardware_profile() -> dict[str, Any]:
    """Return a strict v5-compatible profile when collection is unavailable."""

    return {
        "profile_version": PROFILE_VERSION,
        "os_family": "other",
        "os_version": None,
        "architecture": "other",
        "cpu_model": None,
        "logical_cpu_count": None,
        "ram_total_mb": None,
        "gpu_adapters": [],
        "encoder_capabilities": [],
    }


def sanitize_hardware_profile(value: Any) -> dict[str, Any]:
    """Reduce any candidate mapping to the exact bounded network allowlist."""

    source = value if isinstance(value, Mapping) else {}
    os_family = str(source.get("os_family", "") or "").strip().casefold()
    if os_family not in OS_FAMILIES:
        os_family = "other"
    architecture = str(source.get("architecture", "") or "").strip().casefold()
    if architecture not in ARCHITECTURES:
        architecture = "other"
    version_parts = _VERSION_NUMBER_RE.findall(
        str(source.get("os_version", "") or "")
    )[:2]
    cpu_source = source.get("cpu_model")
    cpu_model = str(cpu_source).strip()[:64] if cpu_source is not None else None
    if cpu_model and (
        _LONG_IDENTIFIER_RE.search(cpu_model)
        or _FORBIDDEN_MODEL_WORD_RE.search(cpu_model)
        or not _COARSE_CPU_MODEL_RE.fullmatch(cpu_model)
    ):
        cpu_model = None
    return {
        "profile_version": PROFILE_VERSION,
        "os_family": os_family,
        "os_version": ".".join(version_parts) if version_parts else None,
        "architecture": architecture,
        "cpu_model": cpu_model or None,
        "logical_cpu_count": _strict_int_or_none(
            source.get("logical_cpu_count"), 1, MAX_LOGICAL_CPUS
        ),
        "ram_total_mb": _strict_int_or_none(
            source.get("ram_total_mb"), 256, MAX_RAM_MB
        ),
        "gpu_adapters": _sanitize_gpu_adapters(source.get("gpu_adapters")),
        "encoder_capabilities": _encoder_capabilities(
            source.get("encoder_capabilities")
        ),
    }


def collect_hardware_profile(
    *,
    encoder_capabilities: Any = None,
    gpu_adapters: Any = None,
    platform_module: Any = platform,
    os_module: Any = os,
    psutil_module: Any = _DEFAULT_PSUTIL,
) -> dict[str, Any]:
    """Collect one coarse snapshot without commands, paths, env, or IDs."""

    try:
        family = _os_family(platform_module)
        architecture = _architecture(platform_module)
        cpu_model = _coarse_cpu_model(platform_module, family, architecture)
        capabilities = _encoder_capabilities(encoder_capabilities)
        adapters = _sanitize_gpu_adapters(gpu_adapters)
        if not adapters:
            adapters = _derived_gpu_adapters(capabilities, cpu_model)
        return sanitize_hardware_profile(
            {
                "os_family": family,
                "os_version": _coarse_os_version(platform_module, family),
                "architecture": architecture,
                "cpu_model": cpu_model,
                "logical_cpu_count": _logical_cpu_count(os_module),
                "ram_total_mb": _ram_total_mb(psutil_module),
                "gpu_adapters": adapters,
                "encoder_capabilities": capabilities,
            }
        )
    except Exception:
        return unavailable_hardware_profile()


def is_valid_hardware_profile(value: Any) -> bool:
    """Strict validator used by the schema-v5 event builder."""

    if not isinstance(value, Mapping) or set(value) != PROFILE_FIELDS:
        return False
    if value.get("profile_version") != PROFILE_VERSION:
        return False
    if value.get("os_family") not in OS_FAMILIES:
        return False
    version = value.get("os_version")
    if version is not None and (
        not isinstance(version, str)
        or len(version) > 16
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", version)
    ):
        return False
    if value.get("architecture") not in ARCHITECTURES:
        return False
    cpu_model = value.get("cpu_model")
    if cpu_model is not None and (
        not isinstance(cpu_model, str)
        or not cpu_model
        or len(cpu_model) > 64
        or _LONG_IDENTIFIER_RE.search(cpu_model)
        or _FORBIDDEN_MODEL_WORD_RE.search(cpu_model)
        or not _COARSE_CPU_MODEL_RE.fullmatch(cpu_model)
    ):
        return False
    logical = value.get("logical_cpu_count")
    if logical is not None and _strict_int_or_none(
        logical, 1, MAX_LOGICAL_CPUS
    ) != logical:
        return False
    ram = value.get("ram_total_mb")
    if ram is not None and _strict_int_or_none(ram, 256, MAX_RAM_MB) != ram:
        return False
    adapters = value.get("gpu_adapters")
    if not isinstance(adapters, list) or len(adapters) > MAX_GPU_ADAPTERS:
        return False
    for adapter in adapters:
        if not isinstance(adapter, Mapping) or set(adapter) != GPU_FIELDS:
            return False
        if adapter.get("vendor") not in GPU_VENDORS:
            return False
        model = adapter.get("model")
        if (
            not isinstance(model, str)
            or not _SAFE_GPU_MODEL_RE.fullmatch(model)
            or _LONG_IDENTIFIER_RE.search(model)
            or _FORBIDDEN_MODEL_WORD_RE.search(model)
        ):
            return False
        memory = adapter.get("memory_mb")
        if memory is not None and _strict_int_or_none(
            memory, 256, MAX_GPU_MEMORY_MB
        ) != memory:
            return False
    adapter_keys = [
        (item["vendor"], item["model"], item["memory_mb"] or 0)
        for item in adapters
    ]
    if adapter_keys != sorted(adapter_keys) or len(adapter_keys) != len(
        set(adapter_keys)
    ):
        return False
    capabilities = value.get("encoder_capabilities")
    if not isinstance(capabilities, list) or len(
        capabilities
    ) > MAX_ENCODER_CAPABILITIES:
        return False
    if not all(
        isinstance(item, str) and item in ENCODER_CAPABILITIES
        for item in capabilities
    ):
        return False
    return capabilities == sorted(set(capabilities))


__all__ = [
    "ENCODER_CAPABILITIES",
    "PROFILE_VERSION",
    "collect_hardware_profile",
    "is_valid_hardware_profile",
    "sanitize_hardware_profile",
    "unavailable_hardware_profile",
]
