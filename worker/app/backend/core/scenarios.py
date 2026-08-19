"""Phase 8 (V1.0.2.15): Scenario presets for TC02/TC04 reframe.

Each scenario pre-derives lens range + compositions + default encoder
based on common video production use cases. Inspired by the reference
implementation in green.sj88ai.com.
"""
from __future__ import annotations
from typing import Dict, Any, List


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "presenter": {
        "label": "Presenter (24-50mm)",
        "lens_mm_min": 24.0,
        "lens_mm_max": 50.0,
        "compositions": ["center", "right"],
        "default_encoder": "nvenc",
        "description": "Single-person presentation, medium shots",
    },
    "interview": {
        "label": "Interview (50-85mm)",
        "lens_mm_min": 50.0,
        "lens_mm_max": 85.0,
        "compositions": ["center", "left", "right"],
        "default_encoder": "nvenc",
        "description": "Close-up interview, flattering compression",
    },
    "review": {
        "label": "Review (16-35mm)",
        "lens_mm_min": 16.0,
        "lens_mm_max": 35.0,
        "compositions": ["center"],
        "default_encoder": "libx264",
        "description": "Product review, wide context",
    },
    "group": {
        "label": "Group (16-28mm)",
        "lens_mm_min": 16.0,
        "lens_mm_max": 28.0,
        "compositions": ["left", "center", "right"],
        "default_encoder": "nvenc",
        "description": "Group shots, wide angle",
    },
    "wide": {
        "label": "Wide (16-24mm)",
        "lens_mm_min": 16.0,
        "lens_mm_max": 24.0,
        "compositions": ["center"],
        "default_encoder": "nvenc",
        "description": "Establishing / landscape shots",
    },
    "portrait": {
        "label": "Portrait (50-85mm)",
        "lens_mm_min": 50.0,
        "lens_mm_max": 85.0,
        "compositions": ["center"],
        "default_encoder": "nvenc",
        "description": "Portrait mode, shallow depth of field",
    },
}

DEFAULT_SCENARIO = "presenter"


def get_scenario(key: str) -> Dict[str, Any]:
    """Return scenario config by key, or DEFAULT_SCENARIO if unknown."""
    return SCENARIOS.get(str(key or "").strip().lower(), SCENARIOS[DEFAULT_SCENARIO])


def scenario_keys() -> List[str]:
    """Return all scenario keys in display order."""
    return list(SCENARIOS.keys())


def scenario_labels() -> Dict[str, str]:
    """Return {key: label} for UI dropdowns."""
    return {k: v["label"] for k, v in SCENARIOS.items()}
