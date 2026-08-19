"""TC06 product-folder discovery shared by GUI, CLI, and pipeline."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from .auto_dragdrop import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS


TC06_OUTPUT_DIR_NAME = "tc06_output"


@dataclass(frozen=True)
class ProductFolderLayout:
    root: str
    products: Tuple[str, ...]
    backgrounds: Tuple[str, ...]
    audios: Tuple[str, ...]

    @property
    def output_dir(self) -> str:
        return str(Path(self.root) / TC06_OUTPUT_DIR_NAME)

    @property
    def chroma_dir(self) -> str:
        return str(Path(self.output_dir) / "chroma")

    @property
    def final_dir(self) -> str:
        return str(Path(self.output_dir) / "final")


def _normalized(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _role_directories(root: Path) -> dict[str, Path]:
    children = {
        child.name.casefold(): child
        for child in root.iterdir()
        if child.is_dir()
    }
    return {
        role: children[role]
        for role in ("product", "bg", "audio")
        if role in children
    }


def _files(directory: Path, extensions: Iterable[str]) -> Tuple[str, ...]:
    allowed = {str(ext).casefold() for ext in extensions}
    return tuple(
        str(path.resolve())
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file() and path.suffix.casefold() in allowed
    )


def inspect_product_folder(path: os.PathLike[str] | str) -> ProductFolderLayout:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"TC06 product folder does not exist: {root}")

    roles = _role_directories(root)
    missing = [role for role in ("product", "bg", "audio") if role not in roles]
    if missing:
        raise ValueError(
            f"TC06 product folder {root} missing role directories: {', '.join(missing)}"
        )

    products = _files(roles["product"], VIDEO_EXTS)
    backgrounds = _files(roles["bg"], VIDEO_EXTS | IMAGE_EXTS)
    audios = _files(roles["audio"], AUDIO_EXTS)
    empty = [
        role
        for role, values in (
            ("product", products),
            ("bg", backgrounds),
            ("audio", audios),
        )
        if not values
    ]
    if empty:
        raise ValueError(
            f"TC06 product folder {root} has no supported files in: {', '.join(empty)}"
        )
    return ProductFolderLayout(
        root=str(root),
        products=products,
        backgrounds=backgrounds,
        audios=audios,
    )


def resolve_product_folders(
    selected_roots: Sequence[os.PathLike[str] | str],
) -> Tuple[List[ProductFolderLayout], List[str]]:
    """Resolve direct product folders or parent roots, preserving fail-closed errors."""

    layouts: List[ProductFolderLayout] = []
    errors: List[str] = []
    seen_selected: set[str] = set()
    seen_products: set[str] = set()

    for raw in selected_roots:
        selected = Path(raw).expanduser().resolve()
        selected_key = _normalized(selected)
        if selected_key in seen_selected:
            continue
        seen_selected.add(selected_key)
        if not selected.is_dir():
            errors.append(f"TC06 selected root is not a directory: {selected}")
            continue

        try:
            present_roles = _role_directories(selected)
        except OSError as exc:
            errors.append(f"TC06 cannot read selected root {selected}: {exc}")
            continue

        candidates: List[Path]
        if present_roles:
            # Any role marker means the user selected a product folder.  A
            # partially named layout must fail here instead of accidentally
            # treating product/bg/audio as separate products.
            candidates = [selected]
        else:
            try:
                candidates = [
                    child
                    for child in sorted(
                        selected.iterdir(), key=lambda item: item.name.casefold()
                    )
                    if child.is_dir()
                    and not child.name.startswith(".")
                    and child.name.casefold() != TC06_OUTPUT_DIR_NAME.casefold()
                ]
            except OSError as exc:
                errors.append(f"TC06 cannot enumerate parent root {selected}: {exc}")
                continue
            if not candidates:
                errors.append(f"TC06 parent root has no product folders: {selected}")
                continue

        for candidate in candidates:
            try:
                layout = inspect_product_folder(candidate)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
                continue
            key = _normalized(layout.root)
            if key not in seen_products:
                seen_products.add(key)
                layouts.append(layout)

    if not selected_roots:
        errors.append("TC06 requires at least one product root/folder")
    return layouts, errors


def safe_stem(path: os.PathLike[str] | str) -> str:
    stem = Path(path).stem.strip()
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem)
    return cleaned[:100] or "audio"


def final_output_path(
    layout: ProductFolderLayout,
    audio: str,
    index: int,
    run_stamp: str,
) -> str:
    stamp = "".join(ch for ch in str(run_stamp) if ch.isdigit() or ch == "_")
    if not stamp:
        raise ValueError("TC06 run_stamp must not be empty")
    return str(
        Path(layout.final_dir)
        / f"tc06_{stamp}_{index:03d}_{safe_stem(audio)}.mp4"
    )
