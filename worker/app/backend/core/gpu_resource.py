"""Optional, privacy-safe aggregate NVIDIA resource sampling.

Only utilization percentage and used memory are returned.  Device names,
UUIDs, serials, PIDs, paths, and command text never enter the returned value.
The provider is deliberately fail-open and permanently disables itself after
the first runtime failure so telemetry cannot repeatedly disturb rendering.
"""
from __future__ import annotations

import importlib
import math
import subprocess
import sys
import threading
from typing import Any, Mapping, Optional


_NVIDIA_SMI_ARGV = (
    "nvidia-smi",
    "--query-gpu=utilization.gpu,memory.used",
    "--format=csv,noheader,nounits",
)
_MAX_GPU_MEMORY_MB = 1_048_576.0


def _bounded_number(value: Any, high: float) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0.0 or number > high:
        return None
    return round(number, 3)


def sanitize_gpu_sample(value: Any) -> dict[str, Optional[float]]:
    """Reduce an injected provider result to the exact two-field contract."""

    source = value if isinstance(value, Mapping) else {}
    return {
        "gpu_percent": _bounded_number(source.get("gpu_percent"), 100.0),
        "gpu_memory_mb": _bounded_number(
            source.get("gpu_memory_mb"), _MAX_GPU_MEMORY_MB
        ),
    }


class GpuResourceSampler:
    """Sample aggregate NVIDIA load through pynvml or a fixed Windows query."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._disabled = False
        self._nvml: Any = None
        self._nvml_ready = False
        self._nvml_checked = False

    @property
    def disabled(self) -> bool:
        with self._lock:
            return self._disabled

    def _disable(self) -> None:
        self._disabled = True

    def _load_nvml(self) -> Any:
        if self._nvml_checked:
            return self._nvml
        self._nvml_checked = True
        try:
            self._nvml = importlib.import_module("pynvml")
            self._nvml.nvmlInit()
            self._nvml_ready = True
        except Exception:
            self._nvml = None
            self._nvml_ready = False
        return self._nvml

    def _sample_nvml(self) -> Optional[dict[str, float]]:
        nvml = self._load_nvml()
        if nvml is None or not self._nvml_ready:
            return None
        count = int(nvml.nvmlDeviceGetCount())
        if count <= 0:
            return None
        utilization: list[float] = []
        used_memory_mb = 0.0
        for index in range(min(count, 32)):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            rates = nvml.nvmlDeviceGetUtilizationRates(handle)
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            utilization.append(float(rates.gpu))
            used_memory_mb += float(memory.used) / (1024.0 * 1024.0)
        return {
            "gpu_percent": max(utilization),
            "gpu_memory_mb": used_memory_mb,
        }

    @staticmethod
    def _sample_nvidia_smi() -> Optional[dict[str, float]]:
        if sys.platform != "win32":
            return None
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        completed = subprocess.run(
            list(_NVIDIA_SMI_ARGV),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            creationflags=creationflags,
        )
        if completed.returncode != 0:
            raise RuntimeError("gpu_sampler_unavailable")
        utilization: list[float] = []
        used_memory_mb = 0.0
        for line in str(completed.stdout or "").splitlines()[:32]:
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 2:
                raise ValueError("gpu_sampler_invalid_output")
            utilization.append(float(parts[0]))
            used_memory_mb += float(parts[1])
        if not utilization:
            raise ValueError("gpu_sampler_empty_output")
        return {
            "gpu_percent": max(utilization),
            "gpu_memory_mb": used_memory_mb,
        }

    def __call__(self) -> Optional[dict[str, float]]:
        with self._lock:
            if self._disabled:
                return None
            try:
                raw = self._sample_nvml()
                if raw is None:
                    raw = self._sample_nvidia_smi()
                if raw is None:
                    # Unsupported hardware/platform is normal, not a failure.
                    return None
                safe = sanitize_gpu_sample(raw)
                if safe["gpu_percent"] is None and safe["gpu_memory_mb"] is None:
                    raise ValueError("gpu_sampler_invalid_values")
                return {
                    key: float(value)
                    for key, value in safe.items()
                    if value is not None
                }
            except Exception:
                self._disable()
                return None


_DEFAULT_SAMPLER = GpuResourceSampler()


def sample_gpu_resource() -> Optional[dict[str, float]]:
    """Return one aggregate sample or ``None`` without raising."""

    return _DEFAULT_SAMPLER()
