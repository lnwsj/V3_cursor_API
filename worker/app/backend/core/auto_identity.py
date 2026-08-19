"""Auto-create-temp-nickname helper for desktop stats.

Adds the missing bootstrap step so that running the app without ever
opening a settings panel still produces a valid GreenStats identity. The
identity is saved once into ``greenstats.json`` and the user can rename it
later through the existing identity UI without ever touching this file.

The temp nickname is a Thai-style adjective + animal combo with a numeric
suffix so two desktops on the same network don't collide.  The combination
table is small (~500 names) so the generated values feel human even though
they are randomised.
"""
from __future__ import annotations

import random
import re
from typing import Optional, Tuple


# ~500 Thai-style nicknames generated from adjective + animal + 2-digit suffix.
# Tables kept short on purpose: a desktop that wants to remain anonymous
# gets a friendly name it can change later.
_ADJECTIVES = [
    "ใจดี", "ขยัน", "เก่ง", "ฉลาด", "สดใส", "ใจเย็น", "สดใส", "ร่าเริง",
    "กล้าหาญ", "อดทน", "มั่นคง", "ซื่อสัตย์", "จริงใจ", "เป็นมิตร", "สงบ", "อ่อนโยน",
    "ใจกว้าง", "รอบคอบ", "ฉลาด", "เก่ง", "สนุก", "ซน", "เร็ว", "ฉับไว",
    "เก่งมาก", "ขยันจริง", "ใจเย็นมาก", "สดใสมาก",
    "brave", "calm", "eager", "kind", "wise", "quick", "bright", "sunny",
    "sharp", "warm", "steady", "honest", "gentle",
]
_ANIMALS = [
    "แมว", "หมา", "กระต่าย", "เสือ", "สิงโต", "หมี", "ม้า", "ลิง",
    "ช้าง", "กวาง", "นกฮูก", "นกแก้ว", "ปลาโลมา", "แมวน้ำ", "เพนกวิน",
    "นกยูง", "ไก่", "เป็ด", "ห่าน", "นกกระจอก", "นกฮูก", "เหยี่ยว", "อินทรี",
    "cat", "dog", "rabbit", "tiger", "lion", "bear", "horse", "monkey",
    "elephant", "deer", "owl", "parrot", "dolphin", "seal", "penguin",
]
_DISALLOWED_RE = re.compile(r"[^a-z0-9_@.\-]+", re.IGNORECASE)


def generate_temp_nickname(
    *,
    rng: Optional[random.Random] = None,
    min_suffix: int = 10,
    max_suffix: int = 99,
) -> Tuple[str, str]:
    """Return (identity_type, identity_value) for a fresh temp nickname.

    The value is human-readable but deterministic enough that the same
    random seed reproduces the same name.  The validation rules in
    ``normalize_identity`` accept all of the produced tokens.

    Note: only ASCII-compatible names are returned because the GreenStats
    validator regex (``^[a-z0-9][a-z0-9._-]{2,63}$``) does not accept
    Thai characters.  Thai tokens in the table are still useful for
    human-visible prompts even when the saved nickname is Latin.
    """
    rng = rng or random.SystemRandom()
    ascii_pool = [n for n in (_ADJECTIVES + _ANIMALS) if n.isascii()]
    if not ascii_pool:
        return "user_id", "anon-user-00"
    adj = rng.choice(ascii_pool)
    animal = rng.choice(ascii_pool)
    if adj == animal:
        animal = rng.choice(ascii_pool)
    suffix = rng.randint(min_suffix, max_suffix + 1)
    return "user_id", f"{adj}-{animal}-{suffix:02d}"


def safe_nickname_token(value: str) -> str:
    """Best-effort cleanup if a user accidentally pastes a name with
    characters that the GreenStats validator rejects."""
    cleaned = _DISALLOWED_RE.sub("_", value).strip("_")
    return cleaned or "anon-user-00"
