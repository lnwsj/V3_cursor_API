"""Opt-in controller for strict evidence from the packaged Tk application.

The normal desktop UI never enters this path.  It is enabled only by an
explicit ``--strict-evidence-spec`` argument and drives the same DropZone,
SettingsPanel, Render button, Worker, and PipelineResult objects a user sees.
Screenshots are captured from coordinates reported by the live Tk root, so the
capture does not depend on external WinRT/WGC window enumeration.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any, Dict, Mapping, Sequence

REQUIRED_STEPS = (
    "01_open_page",
    "02_input_ready",
    "03_click_generate",
    "04_result_state",
    "05_audio_ready_or_error",
)

TAB_MAP = {
    "TC01": ("tab_single", 0),
    "TC02": ("tab_reframe", 1),
    "TC03": ("tab_batch", 2),
    "TC04": ("tab_rebatch", 3),
    "TC05": ("tab_reframe_only", 4),
    "TC06": ("tab_video_loop", 5),
}
REQUIRED_CHROMA_DEFAULT_KEYS = frozenset(
    {
        "width",
        "height",
        "fps",
        "bitrate",
        "encoder",
        "preset",
        "key_color",
        "similarity",
        "blend",
        "despill",
    }
)
STRICT_EVIDENCE_ENV = "GREENPC_ENABLE_STRICT_EVIDENCE"
RUNTIME_PROBE_TIMEOUT_SEC = 30.0
TC06_SCENARIOS = frozenset(
    {
        "render",
        "validate",
        "preview",
        "missing_role",
        "layout_drift",
        "stop_chroma",
        "resume_chroma",
        "inject_folder",
    }
)
_TC06_ENGINE_AUDIT_SCENARIOS = frozenset(
    {"stop_chroma", "resume_chroma", "inject_folder"}
)
_TC06_MUTATING_SCENARIOS = frozenset({"layout_drift"})


class PackagedEvidenceError(RuntimeError):
    """Raised when evidence collection cannot continue without guessing."""


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root.resolve()), str(candidate.resolve()))) == str(
            root.resolve()
        )
    except (OSError, ValueError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class PackagedEvidenceController:
    """Drive one testcase through the real packaged Tk surface."""

    def __init__(self, root: Any, app: Any, spec_path: os.PathLike[str] | str):
        self.root = root
        self.app = app
        self.spec_path = Path(spec_path).resolve()
        self.spec = self._load_spec()
        self.report_dir = Path(self.spec["report_dir"]).resolve()
        self.pair_id = str(self.spec["pair_id"])
        self.tc_id = str(self.spec["tc_id"])
        self.timeout_sec = float(self.spec.get("timeout_sec", 900.0))
        self.started_at = time.monotonic()
        self.tab: Any = None
        self.screenshots: list[str] = []
        self.states: Dict[str, Any] = {}
        self.ui_assertions: Dict[str, Any] = {}
        self.finished = False
        self._poll_after_id: Any = None
        self._original_ffmpeg_run: Any = None
        self._encoder_fault_injected = False
        self._encoder_audit_started = False
        self._encoder_attempts_snapshot: list[Dict[str, Any]] = []
        self._runtime_restorers: list[Any] = []
        self._stop_due = threading.Event()
        self._stop_acknowledged = threading.Event()
        self._stop_button_invoked = False
        self._stop_hook_error = ""
        self._stop_successful_chroma = 0
        self._preview_files_before: set[str] = set()
        self._preview_button_invoked = False
        self.runtime_evidence_errors: list[str] = []

    def _load_spec(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.spec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PackagedEvidenceError(f"cannot read evidence spec: {exc}") from exc
        if not isinstance(value, dict):
            raise PackagedEvidenceError("evidence spec must be a JSON object")
        required = ("report_dir", "pair_id", "request_id", "tc_id", "inputs", "settings")
        missing = [name for name in required if name not in value]
        if missing:
            raise PackagedEvidenceError("evidence spec missing: " + ", ".join(missing))
        if value["tc_id"] not in TAB_MAP:
            raise PackagedEvidenceError(f"unsupported tc_id: {value['tc_id']!r}")
        if not isinstance(value["inputs"], dict) or not isinstance(value["settings"], dict):
            raise PackagedEvidenceError("inputs and settings must be JSON objects")
        expected_ui_rejection = value.get(
            "expected_ui_validation_rejection",
            False,
        )
        if not isinstance(expected_ui_rejection, bool):
            raise PackagedEvidenceError(
                "expected_ui_validation_rejection must be a boolean"
            )
        if expected_ui_rejection:
            if value["tc_id"] != "TC06":
                raise PackagedEvidenceError(
                    "expected_ui_validation_rejection is supported only for TC06"
                )
            if value.get("encoder_expectation") or value.get(
                "force_hardware_failure_once"
            ):
                raise PackagedEvidenceError(
                    "expected_ui_validation_rejection cannot request encoder evidence"
                )
        raw_scenario = value.get("tc06_scenario", "render")
        if not isinstance(raw_scenario, str):
            raise PackagedEvidenceError("tc06_scenario must be a string")
        scenario = raw_scenario.strip().casefold()
        if scenario not in TC06_SCENARIOS:
            raise PackagedEvidenceError(
                "unsupported tc06_scenario: "
                f"{raw_scenario!r}; expected one of {sorted(TC06_SCENARIOS)}"
            )
        if expected_ui_rejection:
            if scenario not in {"render", "missing_role"}:
                raise PackagedEvidenceError(
                    "expected_ui_validation_rejection conflicts with "
                    f"tc06_scenario={scenario}"
                )
            scenario = "missing_role"
        if scenario != "render" and value["tc_id"] != "TC06":
            raise PackagedEvidenceError(
                "non-render tc06_scenario is supported only for TC06"
            )

        isolated = value.get("tc06_isolated_root", False)
        if not isinstance(isolated, bool):
            raise PackagedEvidenceError("tc06_isolated_root must be a boolean")
        if scenario in _TC06_MUTATING_SCENARIOS and not isolated:
            raise PackagedEvidenceError(
                f"tc06_scenario={scenario} requires tc06_isolated_root=true"
            )

        stop_after = value.get("tc06_stop_after_chroma")
        expected_resumed = value.get("tc06_expected_resumed_chroma")
        for key, raw, required_scenario in (
            ("tc06_stop_after_chroma", stop_after, "stop_chroma"),
            (
                "tc06_expected_resumed_chroma",
                expected_resumed,
                "resume_chroma",
            ),
        ):
            if raw is not None and (
                not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0
            ):
                raise PackagedEvidenceError(f"{key} must be a positive integer")
            if scenario == required_scenario and raw is None:
                raise PackagedEvidenceError(
                    f"tc06_scenario={required_scenario} requires {key}"
                )
            if scenario != required_scenario and raw is not None:
                raise PackagedEvidenceError(
                    f"{key} is allowed only with tc06_scenario={required_scenario}"
                )

        inject_folder = value.get("tc06_inject_folder")
        if inject_folder is not None and not isinstance(inject_folder, str):
            raise PackagedEvidenceError("tc06_inject_folder must be a string")
        inject_folder = str(inject_folder or "").strip()
        if scenario == "inject_folder":
            if (
                not inject_folder
                or inject_folder in {".", ".."}
                or Path(inject_folder).name != inject_folder
                or "/" in inject_folder
                or "\\" in inject_folder
            ):
                raise PackagedEvidenceError(
                    "tc06_inject_folder must be one safe product-folder basename"
                )
        elif inject_folder:
            raise PackagedEvidenceError(
                "tc06_inject_folder is allowed only with "
                "tc06_scenario=inject_folder"
            )

        no_engine_scenarios = {
            "validate",
            "preview",
            "missing_role",
            "layout_drift",
        }
        if scenario in no_engine_scenarios and (
            value.get("encoder_expectation")
            or value.get("force_hardware_failure_once")
        ):
            raise PackagedEvidenceError(
                f"tc06_scenario={scenario} cannot request encoder evidence"
            )
        if scenario in _TC06_ENGINE_AUDIT_SCENARIOS and value.get(
            "force_hardware_failure_once"
        ):
            raise PackagedEvidenceError(
                f"tc06_scenario={scenario} cannot inject encoder fallback"
            )
        value["tc06_scenario"] = scenario
        value["tc06_isolated_root"] = isolated
        if inject_folder:
            value["tc06_inject_folder"] = inject_folder
        return value

    @property
    def tc06_scenario(self) -> str:
        return str(self.spec.get("tc06_scenario", "render"))

    @property
    def result_path(self) -> Path:
        return self.report_dir / "logs" / f"{self.pair_id}__packaged_result.json"

    @property
    def job_history_path(self) -> Path:
        return self.report_dir / "logs" / f"{self.pair_id}__job_history.json"

    @property
    def states_path(self) -> Path:
        return self.report_dir / "logs" / f"{self.pair_id}__ui_states.json"

    @property
    def runtime_probe_path(self) -> Path:
        return self.report_dir / "api" / f"{self.pair_id}__runtime_ffmpeg.json"

    @property
    def runtime_log_path(self) -> Path:
        return self.report_dir / "logs" / f"{self.pair_id}__ui_runtime.log"

    @property
    def runtime_capture_required(self) -> bool:
        return isinstance(self.spec.get("runtime_ffmpeg_contract"), Mapping)

    def start(self) -> None:
        try:
            if os.environ.get(STRICT_EVIDENCE_ENV, "").strip() != "1":
                raise PackagedEvidenceError(
                    "strict evidence controller is disabled; "
                    f"set {STRICT_EVIDENCE_ENV}=1 only in an isolated QA run"
                )
            self._validate_paths()
            if self.runtime_capture_required:
                self._capture_runtime_probe()
            attr_name, tab_index = TAB_MAP[self.tc_id]
            self.tab = getattr(self.app, attr_name)
            output_dir = Path(self.spec.get("output_dir") or (
                self.report_dir / "logs" / "outputs" / self.tc_id
            )).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

            def evidence_output_dir(_tab: Any) -> str:
                return str(output_dir)

            self.tab.get_output_dir = MethodType(evidence_output_dir, self.tab)
            self.app.notebook.select(tab_index)
            geometry = str(self.spec.get("window_geometry") or "1500x950+32+32")
            self.root.geometry(geometry)
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass
            self.root.update()
            if bool(self.spec.get("assert_chroma_defaults_reset", False)):
                self._verify_chroma_defaults_and_reset()
            self._scroll(0.0)
            self._capture("01_open_page")

            self._apply_inputs()
            self._apply_settings()
            self.tab._update_required_status()
            self.root.update()
            self._scroll(0.0)
            self._capture("02_input_ready")

            scenario = self.tc06_scenario
            if scenario == "validate":
                self._scroll(0.08)
                self.root.update()
                assertion = self._invoke_tc06_validate_folders()
                self.root.update()
                self._capture("03_click_generate")
                self._finalize_tc06_ui_action(
                    action="validate",
                    assertion=assertion,
                    outputs=[],
                    worker_started=False,
                    engine_started=False,
                )
                return
            if scenario == "preview":
                self._start_tc06_preview_scenario()
                return

            expected_ui_rejection = scenario == "missing_role"
            if not expected_ui_rejection:
                self._prepare_encoder_evidence()
                if scenario == "layout_drift":
                    self._install_tc06_layout_drift_hook()
                elif scenario == "stop_chroma":
                    self._install_tc06_stop_chroma_hook()
                elif scenario == "resume_chroma":
                    self._prepare_tc06_resume_chroma()
                elif scenario == "inject_folder":
                    self._install_tc06_inject_folder_hook()
            self._scroll(float(self.spec.get("action_scroll_fraction", 0.46)))
            self.root.update()
            if expected_ui_rejection:
                rejection = self._invoke_expected_ui_validation_rejection()
            else:
                self.tab._render_btn.invoke()
                rejection = None
            self.root.update()
            self._capture("03_click_generate")
            if expected_ui_rejection:
                self._finalize_ui_validation_rejection(rejection)
                return
            self._poll_after_id = self.root.after(100, self._poll_render)
        except Exception as exc:
            self._fail(exc)

    def _tc06_input_tree_files(self) -> set[str]:
        """Snapshot files below selected TC06 roots without following symlinks."""

        files: set[str] = set()
        for raw_root in self.spec["inputs"].get("product_root", []):
            root = Path(raw_root).resolve()
            if not root.is_dir():
                continue
            try:
                candidates = root.rglob("*")
                for candidate in candidates:
                    if candidate.is_file():
                        files.add(os.path.normcase(str(candidate.resolve())))
            except OSError as exc:
                raise PackagedEvidenceError(
                    f"cannot snapshot TC06 product directory: {root}: {exc}"
                ) from exc
        return files

    def _tc06_layouts(self) -> tuple[Any, ...]:
        from core.tc06_products import resolve_product_folders

        roots = [
            str(Path(raw).resolve())
            for raw in self.spec["inputs"].get("product_root", [])
        ]
        layouts, errors = resolve_product_folders(roots)
        if errors or not layouts:
            detail = "; ".join(errors) if errors else "no valid product folders"
            raise PackagedEvidenceError(
                f"TC06 scenario requires valid product folders: {detail}"
            )
        return tuple(layouts)

    def _find_tc06_button(
        self,
        *,
        attribute: str,
        text_fragment: str,
    ) -> Any:
        """Find one real Tk button without changing the production UI class."""

        explicit = getattr(self.tab, attribute, None)
        if explicit is not None and hasattr(explicit, "invoke"):
            return explicit

        roots = [getattr(self.tab, "parent", None), self.tab]
        pending = [item for item in roots if item is not None]
        seen: set[int] = set()
        matches: list[Any] = []
        expected = text_fragment.casefold()
        while pending:
            widget = pending.pop()
            identity = id(widget)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                text = str(widget.cget("text"))
            except Exception:
                text = ""
            if expected in text.casefold() and hasattr(widget, "invoke"):
                matches.append(widget)
            try:
                pending.extend(list(widget.winfo_children()))
            except Exception:
                pass
        if len(matches) != 1:
            raise PackagedEvidenceError(
                f"expected exactly one TC06 button containing "
                f"{text_fragment!r}, found {len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _capture_messagebox_dialogs() -> tuple[
        list[Dict[str, str]],
        Dict[str, Any],
    ]:
        from tkinter import messagebox

        dialogs: list[Dict[str, str]] = []
        originals = {
            name: getattr(messagebox, name)
            for name in ("showwarning", "showinfo", "showerror")
        }

        def record(kind: str):
            def capture(
                title: Any,
                message: Any,
                *_args: Any,
                **_kwargs: Any,
            ) -> bool:
                dialogs.append(
                    {
                        "kind": kind,
                        "title": str(title),
                        "message": str(message),
                    }
                )
                return False

            return capture

        messagebox.showwarning = record("warning")
        messagebox.showinfo = record("info")
        messagebox.showerror = record("error")
        return dialogs, originals

    @staticmethod
    def _restore_messagebox_dialogs(originals: Mapping[str, Any]) -> None:
        from tkinter import messagebox

        for name, original in originals.items():
            setattr(messagebox, name, original)

    def _invoke_tc06_validate_folders(self) -> Dict[str, Any]:
        """Click Validate folders and machine-check its nonmodal result."""

        layouts = self._tc06_layouts()
        expected_counts = {
            "product_folders": len(layouts),
            "product": sum(len(layout.products) for layout in layouts),
            "background": sum(len(layout.backgrounds) for layout in layouts),
            "audio": sum(len(layout.audios) for layout in layouts),
        }
        files_before = self._tc06_input_tree_files()
        worker_was_busy = bool(self.tab.worker.is_busy())
        button = self._find_tc06_button(
            attribute="_validate_folders_btn",
            text_fragment="Validate folders",
        )
        dialogs, originals = self._capture_messagebox_dialogs()
        invoke_error = ""
        try:
            button.invoke()
        except Exception as exc:
            invoke_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._restore_messagebox_dialogs(originals)

        files_after = self._tc06_input_tree_files()
        info_dialogs = [item for item in dialogs if item["kind"] == "info"]
        error_dialogs = [item for item in dialogs if item["kind"] != "info"]
        message = "\n".join(item["message"] for item in info_dialogs)
        expected_tokens = [
            str(expected_counts["product_folders"]),
            f"Product {expected_counts['product']}",
            f"Background {expected_counts['background']}",
            f"Audio {expected_counts['audio']}",
        ]
        missing_tokens = [token for token in expected_tokens if token not in message]
        created_files = sorted(files_after - files_before)
        errors: list[str] = []
        if invoke_error:
            errors.append(f"Validate button raised: {invoke_error}")
        if worker_was_busy or self.tab.worker.is_busy():
            errors.append("Validate unexpectedly observed a busy Worker")
        if len(info_dialogs) != 1:
            errors.append(
                f"Validate expected one info dialog, observed {len(info_dialogs)}"
            )
        if error_dialogs:
            errors.append("Validate emitted warning/error dialog")
        if missing_tokens:
            errors.append(
                "Validate dialog is missing exact count tokens: "
                + ", ".join(missing_tokens)
            )
        if created_files:
            errors.append("Validate created files unexpectedly")

        assertion = {
            "button_text": str(button.cget("text"))
            if hasattr(button, "cget")
            else "Validate folders",
            "button_invoked": True,
            "dialogs": dialogs,
            "expected_counts": expected_counts,
            "missing_count_tokens": missing_tokens,
            "worker_started": False,
            "preflight_started": False,
            "engine_started": False,
            "created_output_count": len(created_files),
            "created_output_files": created_files,
            "errors": errors,
            "passed": not errors,
        }
        self.ui_assertions["tc06_validate"] = assertion
        return assertion

    def _start_tc06_preview_scenario(self) -> None:
        """Click the real Preview button and wait for its Worker result."""

        self._tc06_layouts()
        if self.tab.worker.is_busy():
            raise PackagedEvidenceError("TC06 Preview Worker is already busy")
        self._preview_files_before = self._tc06_input_tree_files()
        button = self._find_tc06_button(
            attribute="_preview_btn",
            text_fragment="Preview",
        )
        self._scroll(float(self.spec.get("action_scroll_fraction", 0.46)))
        self.root.update()
        button.invoke()
        self._preview_button_invoked = True
        self.root.update()
        self._capture("03_click_generate")
        self._poll_after_id = self.root.after(100, self._poll_tc06_preview)

    def _poll_tc06_preview(self) -> None:
        if self.finished:
            return
        elapsed = time.monotonic() - self.started_at
        if elapsed > self.timeout_sec:
            try:
                self.tab.worker.cancel()
            except Exception:
                pass
            self._fail(
                PackagedEvidenceError(
                    f"TC06 packaged Preview timed out after {elapsed:.1f}s"
                )
            )
            return
        if self.tab.worker.is_busy():
            self._poll_after_id = self.root.after(100, self._poll_tc06_preview)
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass

        preview_path = Path(
            str(getattr(self.tab, "_preview_path", "") or "")
        ).resolve()
        expected_dir = Path(self.tab._get_preview_output_dir()).resolve()
        files_after = self._tc06_input_tree_files()
        created_files = sorted(files_after - self._preview_files_before)
        published_mp4 = [
            path for path in created_files if Path(path).suffix.casefold() == ".mp4"
        ]
        gallery_paths = {
            os.path.normcase(str(Path(path).resolve()))
            for path in getattr(self.tab, "_gallery_paths", [])
        }
        label = getattr(self.tab, "_preview_image_label", None)
        try:
            image_label_text = str(label.cget("text")) if label is not None else ""
        except Exception:
            image_label_text = ""

        errors: list[str] = []
        if not self._preview_button_invoked:
            errors.append("Preview button was not invoked")
        if not preview_path.is_file() or preview_path.stat().st_size <= 0:
            errors.append(f"Preview output is missing or empty: {preview_path}")
        if preview_path.suffix.casefold() != ".png":
            errors.append("Preview output is not PNG")
        if preview_path.parent != expected_dir:
            errors.append(
                f"Preview output directory mismatch: "
                f"expected={expected_dir}, observed={preview_path.parent}"
            )
        if getattr(self.tab, "_preview_photo", None) is None:
            errors.append("Preview PhotoImage was not displayed")
        if image_label_text:
            errors.append("Preview image label still contains placeholder text")
        if os.path.normcase(str(preview_path)) not in gallery_paths:
            errors.append("Preview output is missing from the in-app Gallery")
        if published_mp4:
            errors.append("Preview unexpectedly created Audio-master MP4 output")
        if self.tab.worker.is_busy():
            errors.append("Preview Worker is still busy at terminal capture")

        assertion = {
            "button_invoked": self._preview_button_invoked,
            "worker_started": bool(
                self._preview_button_invoked and preview_path.is_file()
            ),
            "preflight_started": False,
            "engine_started": bool(preview_path.is_file()),
            "preview_path": str(preview_path),
            "preview_sha256": (
                _sha256(preview_path) if preview_path.is_file() else ""
            ),
            "preview_bytes": (
                preview_path.stat().st_size if preview_path.is_file() else 0
            ),
            "expected_output_dir": str(expected_dir),
            "displayed_in_app": getattr(self.tab, "_preview_photo", None) is not None,
            "gallery_contains_preview": (
                os.path.normcase(str(preview_path)) in gallery_paths
            ),
            "created_files": created_files,
            "new_mp4_outputs": published_mp4,
            "errors": errors,
            "passed": not errors,
        }
        self.ui_assertions["tc06_preview"] = assertion
        outputs = [str(preview_path)] if preview_path.is_file() else []
        self._finalize_tc06_ui_action(
            action="preview",
            assertion=assertion,
            outputs=outputs,
            worker_started=assertion["worker_started"],
            engine_started=assertion["engine_started"],
            preview_path=str(preview_path),
        )

    def _finalize_tc06_ui_action(
        self,
        *,
        action: str,
        assertion: Mapping[str, Any],
        outputs: list[str],
        worker_started: bool,
        engine_started: bool,
        preview_path: str = "",
    ) -> None:
        """Persist a complete non-render TC06 action with five UI states."""

        try:
            self._scroll(float(self.spec.get("result_scroll_fraction", 0.55)))
            self._capture("04_result_state")
            self._scroll(1.0)
            self._capture("05_audio_ready_or_error")
            _atomic_write_json(self.job_history_path, [])
            passed = assertion.get("passed") is True
            payload = {
                "success": passed,
                "scenario": action,
                "scenario_verdict": "PASS" if passed else "FAIL",
                "tc_id": self.tc_id,
                "pair_id": self.pair_id,
                "request_id": self.spec["request_id"],
                "job_id": "",
                "job_history_path": str(self.job_history_path),
                "screenshots": list(self.screenshots),
                "states_path": str(self.states_path),
                "elapsed_ms": round(
                    (time.monotonic() - self.started_at) * 1000.0,
                    3,
                ),
                "window_title": str(self.root.title()),
                "ui_assertions": self.ui_assertions,
                "pipeline_result": None,
                "preflight_result": None,
                "worker_started": bool(worker_started),
                "preflight_started": False,
                "engine_started": bool(engine_started),
                "outputs": list(outputs),
                "output_count": len(outputs),
                "artifact_outputs": list(outputs),
                "artifact_count": len(outputs),
                "scenario_details": dict(assertion),
                "action_status": "COMPLETED" if passed else "FAILED",
                "error": "" if passed else "; ".join(assertion.get("errors", [])),
            }
            if preview_path:
                payload["preview_path"] = preview_path
            self._write_result_payload(payload)
            self.finished = True
            self._close_app()
        except Exception as exc:
            self._fail(exc)

    def _invoke_expected_ui_validation_rejection(self) -> Dict[str, Any]:
        """Invoke Render while proving TC06 rejects a missing role on the Tk thread.

        The opt-in evidence path records the same warning that the user would
        see, but suppresses the modal dialog so unattended packaged QA cannot
        block.  Worker.start is guarded during this one click: any attempt is
        recorded and rejected, making an unexpectedly valid input fail closed
        before preflight or an encoder can start.
        """

        from tkinter import messagebox

        dialogs: list[Dict[str, str]] = []
        worker_start_attempts: list[str] = []
        worker = self.tab.worker
        original_dialogs = {
            name: getattr(messagebox, name)
            for name in ("showwarning", "showinfo", "showerror")
        }
        had_instance_start = "start" in getattr(worker, "__dict__", {})
        original_instance_start = getattr(worker, "__dict__", {}).get("start")

        def record_dialog(kind: str):
            def capture(title: Any, message: Any, *_args: Any, **_kwargs: Any) -> bool:
                dialogs.append(
                    {
                        "kind": kind,
                        "title": str(title),
                        "message": str(message),
                    }
                )
                return False

            return capture

        def block_worker_start(*_args: Any, **kwargs: Any) -> bool:
            worker_start_attempts.append(str(kwargs.get("label", "") or "unlabelled"))
            return False

        files_before = self._tc06_input_tree_files()
        try:
            messagebox.showwarning = record_dialog("warning")
            messagebox.showinfo = record_dialog("info")
            messagebox.showerror = record_dialog("error")
            worker.start = block_worker_start
            self.tab._render_btn.invoke()
        finally:
            for name, original in original_dialogs.items():
                setattr(messagebox, name, original)
            if had_instance_start:
                worker.start = original_instance_start
            else:
                try:
                    delattr(worker, "start")
                except AttributeError:
                    pass

        files_after = self._tc06_input_tree_files()
        created_files = sorted(files_after - files_before)
        warnings = [item for item in dialogs if item["kind"] == "warning"]
        error_text = "\n".join(item["message"] for item in warnings)
        marker = "missing role directories:"
        marker_index = error_text.casefold().find(marker)
        missing_roles: list[str] = []
        if marker_index >= 0:
            missing_detail = error_text[marker_index + len(marker) :].casefold()
            missing_roles = [
                role
                for role in ("product", "bg", "audio")
                if role in missing_detail
            ]

        result = getattr(self.tab, "_last_pipeline_result", None)
        record = getattr(self.tab, "_last_job_record", None)
        preflight = getattr(self.tab, "_preflight_last_result", None)
        assertion = {
            "expected": True,
            "observed": bool(warnings and marker_index >= 0 and missing_roles),
            "dialogs": dialogs,
            "missing_roles": missing_roles,
            "worker_start_attempt_count": len(worker_start_attempts),
            "worker_start_attempts": worker_start_attempts,
            "worker_started": False,
            "preflight_started": preflight is not None,
            "engine_started": False,
            "created_output_count": len(created_files),
            "created_output_files": created_files,
            "pipeline_result_present": result is not None,
            "job_record_present": isinstance(record, dict),
        }
        assertion["passed"] = bool(
            assertion["observed"]
            and not worker_start_attempts
            and preflight is None
            and result is None
            and not isinstance(record, dict)
            and not created_files
            and not worker.is_busy()
        )
        self.ui_assertions["ui_validation_rejection"] = assertion
        if not assertion["passed"]:
            raise PackagedEvidenceError(
                "expected TC06 missing-role UI validation rejection was not "
                f"observed safely: {json.dumps(assertion, ensure_ascii=False)}"
            )
        return assertion

    @staticmethod
    def _manifest_sha256(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _patch_runtime_attribute(
        self,
        owner: Any,
        name: str,
        replacement: Any,
    ) -> None:
        namespace = getattr(owner, "__dict__", {})
        had_instance_value = name in namespace
        original_instance_value = namespace.get(name)
        original_resolved_value = getattr(owner, name)
        setattr(owner, name, replacement)

        def restore() -> None:
            if had_instance_value:
                setattr(owner, name, original_instance_value)
            else:
                try:
                    delattr(owner, name)
                except AttributeError:
                    # Modules always own their patched attributes.
                    setattr(owner, name, original_resolved_value)

        self._runtime_restorers.append(restore)

    def _restore_runtime_hooks(self) -> None:
        while self._runtime_restorers:
            restore = self._runtime_restorers.pop()
            try:
                restore()
            except Exception:
                pass

    def _install_tc06_layout_drift_hook(self) -> None:
        """Mutate one isolated Product only after bound preflight succeeds."""

        layouts = self._tc06_layouts()
        first = layouts[0]
        if not first.products:
            raise PackagedEvidenceError("TC06 layout-drift root has no Product")
        assertion: Dict[str, Any] = {
            "isolated_root_confirmed": bool(self.spec["tc06_isolated_root"]),
            "preflight_ok_before_mutation": False,
            "mutation_performed": False,
            "mutation_method": "",
            "source_path": str(first.products[0]),
            "created_path": "",
            "captured_manifest_sha256": "",
            "mutated_manifest_sha256": "",
            "engine_call_attempts": {
                "render_tc01": 0,
                "render_audio_master": 0,
            },
            "errors": [],
            "passed": False,
        }
        self.ui_assertions["tc06_layout_drift"] = assertion

        from core.ffmpeg_runner import FfmpegResult
        from core.pipelines import PipelineResult
        from core.pipelines import tc06_video_loop as tc06_pipeline

        original_tc01 = tc06_pipeline.render_tc01
        original_audio = tc06_pipeline.render_audio_master

        def block_unexpected_tc01(inputs: Any, _callbacks: Any) -> Any:
            assertion["engine_call_attempts"]["render_tc01"] += 1
            expected = max(1, len(getattr(inputs, "products", []) or []))
            return PipelineResult(
                pipeline="TC01",
                expected=expected,
                failed=expected,
                errors=[
                    "strict layout-drift guard blocked an unexpected TC01 call"
                ],
            ).finalize()

        def block_unexpected_audio(*_args: Any, **_kwargs: Any) -> Any:
            assertion["engine_call_attempts"]["render_audio_master"] += 1
            return FfmpegResult(
                success=False,
                returncode=96,
                error=(
                    "strict layout-drift guard blocked an unexpected "
                    "audio-master call"
                ),
            )

        self._patch_runtime_attribute(
            tc06_pipeline,
            "render_tc01",
            block_unexpected_tc01,
        )
        self._patch_runtime_attribute(
            tc06_pipeline,
            "render_audio_master",
            block_unexpected_audio,
        )

        original_builder = self.tab._build_render_target_from_request
        mutation_done = False

        def mutate_then_render(request: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal mutation_done
            if not mutation_done:
                mutation_done = True
                preflight = getattr(self.tab, "_preflight_last_result", None)
                assertion["preflight_ok_before_mutation"] = bool(
                    preflight is not None and getattr(preflight, "ok", False)
                )
                if not assertion["preflight_ok_before_mutation"]:
                    raise PackagedEvidenceError(
                        "layout drift hook ran before a passing preflight"
                    )
                source = Path(first.products[0]).resolve()
                destination = source.with_name(
                    f"__strict_layout_drift_after_preflight_{os.getpid()}"
                    f"{source.suffix.casefold()}"
                )
                if destination.exists():
                    raise PackagedEvidenceError(
                        f"layout drift destination already exists: {destination}"
                    )
                try:
                    os.link(source, destination)
                    method = "hardlink"
                except OSError:
                    shutil.copy2(source, destination)
                    method = "copy2"
                if not destination.is_file() or destination.stat().st_size <= 0:
                    raise PackagedEvidenceError(
                        f"layout drift mutation is missing/empty: {destination}"
                    )
                from core.pipelines.tc06_video_loop import (
                    tc06_layout_manifest,
                )
                from core.tc06_products import resolve_product_folders

                mutated_layouts, errors = resolve_product_folders(
                    [layout.root for layout in layouts]
                )
                if errors:
                    raise PackagedEvidenceError(
                        "cannot resolve mutated TC06 layout: " + "; ".join(errors)
                    )
                captured_manifest = getattr(request, "layout_manifest", ())
                mutated_manifest = tc06_layout_manifest(mutated_layouts)
                assertion.update(
                    {
                        "mutation_performed": True,
                        "mutation_method": method,
                        "created_path": str(destination),
                        "created_bytes": destination.stat().st_size,
                        "created_sha256": _sha256(destination),
                        "captured_manifest_sha256": self._manifest_sha256(
                            captured_manifest
                        ),
                        "mutated_manifest_sha256": self._manifest_sha256(
                            mutated_manifest
                        ),
                    }
                )
                if (
                    assertion["captured_manifest_sha256"]
                    == assertion["mutated_manifest_sha256"]
                ):
                    raise PackagedEvidenceError(
                        "layout drift mutation did not change the manifest"
                    )
            return original_builder(request, *args, **kwargs)

        self._patch_runtime_attribute(
            self.tab,
            "_build_render_target_from_request",
            mutate_then_render,
        )

    def _install_tc06_stop_chroma_hook(self) -> None:
        """Request the real Tk Stop button after the Kth Chroma success."""

        from core.ffmpeg_runner import FfmpegResult, FfmpegRunner

        layouts = self._tc06_layouts()
        first = layouts[0]
        target = int(self.spec["tc06_stop_after_chroma"])
        if target >= len(first.products):
            raise PackagedEvidenceError(
                "stop_chroma is bounded to the first folder's Chroma stage; "
                f"target must be < {len(first.products)}, got {target}"
            )
        assertion: Dict[str, Any] = {
            "expected_k": target,
            "observed_successful_chroma_attempts": 0,
            "stop_button_invoked": False,
            "stop_button_state_before_invoke": "",
            "hook_error": "",
            "errors": [],
            "passed": False,
        }
        self.ui_assertions["tc06_stop_chroma"] = assertion
        chroma_dir = Path(first.chroma_dir).resolve()
        original_run = FfmpegRunner.run
        self._original_ffmpeg_run = original_run
        controller = self

        def run_until_stop(runner: Any, cmd: Any, *args: Any, **kwargs: Any):
            if controller._stop_due.is_set() and not controller._stop_acknowledged.is_set():
                return FfmpegResult(
                    success=False,
                    returncode=130,
                    error="strict Stop request was not acknowledged by Tk",
                    cancelled=True,
                )
            result = original_run(runner, cmd, *args, **kwargs)
            try:
                output_path = Path(str(cmd[-1])).resolve() if cmd else Path()
                is_chroma = bool(cmd) and _inside(chroma_dir, output_path)
            except Exception:
                is_chroma = False
            if bool(getattr(result, "success", False)) and is_chroma:
                controller._stop_successful_chroma += 1
                assertion["observed_successful_chroma_attempts"] = (
                    controller._stop_successful_chroma
                )
                if controller._stop_successful_chroma == target:
                    controller._stop_due.set()
                    if not controller._stop_acknowledged.wait(timeout=10.0):
                        controller._stop_hook_error = (
                            "Tk did not acknowledge Stop within 10 seconds"
                        )
                        assertion["hook_error"] = controller._stop_hook_error
            return result

        FfmpegRunner.run = run_until_stop

    def _service_tc06_stop_request(self) -> None:
        if (
            self.tc06_scenario != "stop_chroma"
            or not self._stop_due.is_set()
            or self._stop_acknowledged.is_set()
        ):
            return
        assertion = self.ui_assertions.get("tc06_stop_chroma", {})
        button = getattr(self.tab, "_stop_btn", None)
        try:
            if button is None or not hasattr(button, "invoke"):
                raise PackagedEvidenceError("TC06 Stop button is not invokable")
            try:
                assertion["stop_button_state_before_invoke"] = str(
                    button.cget("state")
                )
            except Exception:
                assertion["stop_button_state_before_invoke"] = ""
            button.invoke()
            self._stop_button_invoked = True
            assertion["stop_button_invoked"] = True
        except Exception as exc:
            self._stop_hook_error = f"{type(exc).__name__}: {exc}"
            assertion["hook_error"] = self._stop_hook_error
        finally:
            self._stop_acknowledged.set()

    @staticmethod
    def _checkpoint_completed_outputs(out_dir: str) -> Dict[str, Any]:
        from core.render_checkpoint import (
            load_checkpoint_document,
            validate_completed_output,
        )

        loaded = load_checkpoint_document(out_dir)
        data = loaded.data if loaded.kind == "v2" and isinstance(loaded.data, dict) else {}
        tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
        completed: list[str] = []
        invalid: list[str] = []
        for task in tasks.values():
            if not isinstance(task, Mapping) or task.get("status") != "completed":
                continue
            output = str(task.get("output_path", "") or "")
            if output and validate_completed_output(
                output,
                task.get("output_signature") or {},
            ):
                completed.append(str(Path(output).resolve()))
            else:
                invalid.append(output)
        return {
            "kind": loaded.kind,
            "path": loaded.path,
            "reason": loaded.reason,
            "completed_outputs": sorted(completed),
            "completed_count": len(completed),
            "invalid_completed_outputs": invalid,
            "checkpoint_sha256": (
                _sha256(Path(loaded.path))
                if loaded.path and Path(loaded.path).is_file()
                else ""
            ),
        }

    def _prepare_tc06_resume_chroma(self) -> None:
        layouts = self._tc06_layouts()
        expected = int(self.spec["tc06_expected_resumed_chroma"])
        if expected >= len(layouts[0].products):
            raise PackagedEvidenceError(
                "resume_chroma is bounded to a partial first-folder Chroma "
                f"checkpoint; expected must be < {len(layouts[0].products)}"
            )
        checkpoint = self._checkpoint_completed_outputs(layouts[0].chroma_dir)
        errors: list[str] = []
        if checkpoint["kind"] != "v2":
            errors.append(
                f"expected schema-v2 checkpoint, observed {checkpoint['kind']}"
            )
        if checkpoint["completed_count"] != expected:
            errors.append(
                "resume checkpoint count mismatch: "
                f"expected={expected}, observed={checkpoint['completed_count']}"
            )
        if checkpoint["invalid_completed_outputs"]:
            errors.append("resume checkpoint contains invalid completed outputs")
        assertion = {
            "expected_resumed_k": expected,
            "checkpoint_before": checkpoint,
            "checkpoint_validated_before": checkpoint["completed_count"],
            "errors": errors,
            "passed": False,
        }
        self.ui_assertions["tc06_resume_chroma"] = assertion
        if errors:
            raise PackagedEvidenceError("; ".join(errors))

    def _install_tc06_inject_folder_hook(self) -> None:
        """Inject one whole-folder TC01 failure without starting its engine."""

        layouts = self._tc06_layouts()
        folder = str(self.spec["tc06_inject_folder"])
        targets = [
            layout
            for layout in layouts
            if Path(layout.root).name.casefold() == folder.casefold()
        ]
        if len(targets) != 1:
            raise PackagedEvidenceError(
                f"inject_folder expected one product folder {folder!r}, "
                f"found {len(targets)}"
            )
        target = targets[0]
        expected_counts = {
            "chroma_succeeded": sum(len(item.products) for item in layouts)
            - len(target.products),
            "chroma_failed": len(target.products),
            "final_succeeded": sum(len(item.audios) for item in layouts)
            - len(target.audios),
            "final_failed": len(target.audios),
        }
        expected_counts["published_artifacts"] = (
            expected_counts["chroma_succeeded"]
            + expected_counts["final_succeeded"]
        )
        assertion: Dict[str, Any] = {
            "folder": folder,
            "target_root": target.root,
            "expected_counts": expected_counts,
            "injection_count": 0,
            "injected_folder_audio_calls": 0,
            "errors": [],
            "passed": False,
        }
        self.ui_assertions["tc06_inject_folder"] = assertion

        from core.ffmpeg_runner import FfmpegResult
        from core.pipelines import PipelineResult
        from core.pipelines import tc06_video_loop as tc06_pipeline

        original_tc01 = tc06_pipeline.render_tc01
        original_audio = tc06_pipeline.render_audio_master
        target_key = os.path.normcase(str(Path(target.root).resolve()))

        def layout_root_from_chroma(output_dir: Any) -> str:
            try:
                return os.path.normcase(
                    str(Path(str(output_dir)).resolve().parents[1])
                )
            except Exception:
                return ""

        def inject_tc01(inputs: Any, callbacks: Any) -> Any:
            if layout_root_from_chroma(getattr(inputs, "output_dir", "")) != target_key:
                return original_tc01(inputs, callbacks)
            assertion["injection_count"] += 1
            expected = max(1, len(getattr(inputs, "products", []) or []))
            detail = (
                f"strict QA injected whole-folder TC01 failure for {folder}; "
                "this is instrumentation, not a spontaneous runtime defect"
            )
            return PipelineResult(
                pipeline="TC01",
                expected=expected,
                failed=expected,
                errors=[detail],
                metadata={
                    "injected": True,
                    "injected_folder": folder,
                },
            ).finalize()

        def guard_audio(
            clips: Any,
            audio: Any,
            out_path: Any,
            settings: Any,
            **kwargs: Any,
        ) -> Any:
            try:
                audio_root = os.path.normcase(
                    str(Path(str(audio)).resolve().parent.parent)
                )
            except Exception:
                audio_root = ""
            if audio_root == target_key:
                assertion["injected_folder_audio_calls"] += 1
                return FfmpegResult(
                    success=False,
                    returncode=97,
                    error=(
                        f"strict guard blocked unexpected audio-master call "
                        f"for injected folder {folder}"
                    ),
                )
            return original_audio(
                clips,
                audio,
                out_path,
                settings,
                **kwargs,
            )

        self._patch_runtime_attribute(tc06_pipeline, "render_tc01", inject_tc01)
        self._patch_runtime_attribute(
            tc06_pipeline,
            "render_audio_master",
            guard_audio,
        )

    @staticmethod
    def _pipeline_artifact_outputs(result: Any) -> list[str]:
        observed: list[str] = []
        seen: set[str] = set()
        for stage in getattr(result, "stages", []) or []:
            for raw in getattr(stage, "outputs", []) or []:
                path = str(Path(str(raw)).resolve())
                key = os.path.normcase(path)
                if key not in seen:
                    seen.add(key)
                    observed.append(path)
        for raw in getattr(result, "outputs", []) or []:
            path = str(Path(str(raw)).resolve())
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                observed.append(path)
        return observed

    @staticmethod
    def _pipeline_stage(result: Any, name: str) -> Any:
        for stage in getattr(result, "stages", []) or []:
            if str(getattr(stage, "name", "")) == name:
                return stage
        return None

    def _partial_files_below_tc06_roots(self) -> list[str]:
        partials: list[str] = []
        for raw_root in self.spec["inputs"].get("product_root", []):
            root = Path(raw_root).resolve()
            if not root.is_dir():
                continue
            try:
                partials.extend(
                    str(path.resolve())
                    for path in root.rglob("*")
                    if path.is_file() and ".partial." in path.name.casefold()
                )
            except OSError:
                continue
        return sorted(set(partials))

    def _evaluate_tc06_terminal_scenario(
        self,
        result: Any,
    ) -> tuple[Dict[str, Any], bool]:
        scenario = self.tc06_scenario
        status = str(getattr(getattr(result, "status", None), "value", ""))
        artifacts = self._pipeline_artifact_outputs(result)
        attempts = list(self._encoder_attempts_snapshot)
        layouts = self._tc06_layouts()
        expected_artifacts = sum(
            len(layout.products) + len(layout.audios) for layout in layouts
        )
        details = self.ui_assertions.get(f"tc06_{scenario}", {})
        errors = list(details.get("errors", []))

        if scenario == "layout_drift":
            details["pipeline_status"] = status
            details["artifact_count"] = len(artifacts)
            detail_text = "\n".join(
                str(item) for item in getattr(result, "all_errors", []) or []
            )
            if status != "INVALID_INPUT":
                errors.append(f"expected INVALID_INPUT, observed {status}")
            if "layout changed after preflight" not in detail_text:
                errors.append("pipeline error does not identify layout drift")
            if not details.get("mutation_performed"):
                errors.append("layout mutation was not performed")
            if any(details["engine_call_attempts"].values()):
                errors.append("layout guard reached a TC01/audio engine call")
            if attempts:
                errors.append("layout guard recorded encoder attempts")
            if artifacts:
                errors.append("layout guard published artifacts")
            engine_started = False
        elif scenario == "stop_chroma":
            target = int(self.spec["tc06_stop_after_chroma"])
            chroma = self._pipeline_stage(result, "chroma")
            audio = self._pipeline_stage(result, "audio_master")
            checkpoint = self._checkpoint_completed_outputs(layouts[0].chroma_dir)
            partials = self._partial_files_below_tc06_roots()
            observed_k = int(getattr(chroma, "succeeded", -1)) if chroma else -1
            final_count = int(getattr(audio, "succeeded", -1)) if audio else -1
            details.update(
                {
                    "observed_k": observed_k,
                    "final_count": final_count,
                    "checkpoint_after": checkpoint,
                    "checkpoint_validated_count": checkpoint["completed_count"],
                    "encoder_attempt_count": len(attempts),
                    "partial_files": partials,
                    "partial_count": len(partials),
                }
            )
            if status != "CANCELLED":
                errors.append(f"expected CANCELLED, observed {status}")
            if observed_k != target:
                errors.append(
                    f"terminal Chroma count mismatch: expected={target}, "
                    f"observed={observed_k}"
                )
            if final_count != 0:
                errors.append(f"Stop scenario published {final_count} Final outputs")
            if len(artifacts) != target:
                errors.append(
                    f"Stop artifact count mismatch: expected={target}, "
                    f"observed={len(artifacts)}"
                )
            if checkpoint["completed_count"] != target:
                errors.append(
                    "Stop checkpoint does not bind every completed Chroma output"
                )
            if len(attempts) != target:
                errors.append(
                    f"Stop encoder attempt mismatch: expected={target}, "
                    f"observed={len(attempts)}"
                )
            if any(item.get("success") is not True for item in attempts):
                errors.append("Stop encoder audit contains non-success attempts")
            if not self._stop_button_invoked:
                errors.append("real Tk Stop button was not invoked")
            if self._stop_hook_error:
                errors.append(self._stop_hook_error)
            if partials:
                errors.append("Stop left published partial files")
            engine_started = bool(attempts)
        elif scenario == "resume_chroma":
            assertion = self.ui_assertions["tc06_resume_chroma"]
            expected_resumed = int(
                self.spec["tc06_expected_resumed_chroma"]
            )
            initial_outputs = list(
                assertion["checkpoint_before"]["completed_outputs"]
            )
            checkpoint_after = self._checkpoint_completed_outputs(
                layouts[0].chroma_dir
            )
            expected_new_attempts = expected_artifacts - expected_resumed
            artifact_keys = {
                os.path.normcase(str(Path(path).resolve())) for path in artifacts
            }
            resumed_keys = {
                os.path.normcase(str(Path(path).resolve()))
                for path in initial_outputs
            }
            details.update(
                {
                    "pipeline_status": status,
                    "new_encoder_attempt_count": len(attempts),
                    "expected_new_encoder_attempt_count": expected_new_attempts,
                    "combined_unique_artifact_count": len(artifacts),
                    "checkpoint_after": checkpoint_after,
                    "checkpoint_cleared": checkpoint_after["kind"] == "missing",
                    "resumed_outputs_present": resumed_keys.issubset(artifact_keys),
                }
            )
            if status != "SUCCEEDED" or not getattr(result, "is_success", False):
                errors.append(f"expected SUCCEEDED, observed {status}")
            if len(artifacts) != expected_artifacts:
                errors.append(
                    f"resume artifact count mismatch: expected={expected_artifacts}, "
                    f"observed={len(artifacts)}"
                )
            if len(attempts) != expected_new_attempts:
                errors.append(
                    "resume encoder attempt mismatch: "
                    f"expected={expected_new_attempts}, observed={len(attempts)}"
                )
            if any(item.get("success") is not True for item in attempts):
                errors.append("resume encoder audit contains non-success attempts")
            if checkpoint_after["kind"] != "missing":
                errors.append("resume did not clear the TC01 checkpoint")
            if not resumed_keys.issubset(artifact_keys):
                errors.append("validated checkpoint outputs are absent after resume")
            engine_started = bool(attempts)
        elif scenario == "inject_folder":
            chroma = self._pipeline_stage(result, "chroma")
            audio = self._pipeline_stage(result, "audio_master")
            expected = details["expected_counts"]
            observed = {
                "chroma_succeeded": int(getattr(chroma, "succeeded", -1))
                if chroma
                else -1,
                "chroma_failed": int(getattr(chroma, "failed", -1))
                if chroma
                else -1,
                "final_succeeded": int(getattr(audio, "succeeded", -1))
                if audio
                else -1,
                "final_failed": int(getattr(audio, "failed", -1))
                if audio
                else -1,
                "published_artifacts": len(artifacts),
            }
            details.update(
                {
                    "pipeline_status": status,
                    "observed_counts": observed,
                    "encoder_attempt_count": len(attempts),
                }
            )
            if status != "PARTIAL":
                errors.append(f"expected PARTIAL, observed {status}")
            for name, expected_value in expected.items():
                if observed.get(name) != expected_value:
                    errors.append(
                        f"injected count mismatch {name}: "
                        f"expected={expected_value}, observed={observed.get(name)}"
                    )
            if details.get("injection_count") != 1:
                errors.append("whole-folder injection did not occur exactly once")
            if details.get("injected_folder_audio_calls") != 0:
                errors.append("injected folder reached Audio-master engine")
            if len(attempts) != expected["published_artifacts"]:
                errors.append(
                    "injected encoder attempt mismatch: "
                    f"expected={expected['published_artifacts']}, "
                    f"observed={len(attempts)}"
                )
            if any(item.get("success") is not True for item in attempts):
                errors.append("injected encoder audit contains non-success attempts")
            engine_started = bool(attempts)
        else:
            raise PackagedEvidenceError(
                f"unsupported terminal TC06 scenario: {scenario}"
            )

        details["errors"] = errors
        details["passed"] = not errors
        return details, engine_started

    def _finalize_tc06_terminal_scenario(
        self,
        result: Any,
        record: Mapping[str, Any],
        preflight: Any,
    ) -> None:
        try:
            details, engine_started = self._evaluate_tc06_terminal_scenario(
                result
            )
            self._scroll(float(self.spec.get("result_scroll_fraction", 0.55)))
            self._capture("04_result_state")
            self._scroll(1.0)
            self._capture("05_audio_ready_or_error")
            outputs = [str(Path(path).resolve()) for path in result.outputs]
            artifacts = self._pipeline_artifact_outputs(result)
            functional_success = bool(getattr(result, "is_success", False))
            errors = list(getattr(result, "all_errors", []) or [])
            if self.tc06_scenario == "stop_chroma" and not errors:
                errors = [
                    "TC06 cancelled by the strict QA Stop-after-Chroma scenario"
                ]
            payload = {
                "success": functional_success,
                "scenario": self.tc06_scenario,
                "scenario_verdict": (
                    "PASS" if details.get("passed") is True else "FAIL"
                ),
                "tc_id": self.tc_id,
                "pair_id": self.pair_id,
                "request_id": self.spec["request_id"],
                "job_id": record.get("id", ""),
                "job_history_path": str(self.job_history_path),
                "screenshots": list(self.screenshots),
                "states_path": str(self.states_path),
                "elapsed_ms": round(
                    (time.monotonic() - self.started_at) * 1000.0,
                    3,
                ),
                "window_title": str(self.root.title()),
                "ui_assertions": self.ui_assertions,
                "pipeline_result": result.to_dict(),
                "preflight_result": (
                    preflight.to_dict()
                    if preflight is not None and hasattr(preflight, "to_dict")
                    else None
                ),
                "worker_started": True,
                "preflight_started": bool(preflight is not None),
                "engine_started": bool(engine_started),
                "outputs": outputs,
                "output_count": len(outputs),
                "artifact_outputs": artifacts,
                "artifact_count": len(artifacts),
                "scenario_details": details,
                "error": "; ".join(str(item) for item in errors[:10]),
            }
            if self.tc06_scenario == "layout_drift":
                payload["rejection_stage"] = "pipeline_layout_guard"
            self._write_result_payload(payload)
            self.finished = True
            self._restore_runtime_hooks()
            self._close_app()
        except Exception as exc:
            self._fail(exc)

    def _validate_paths(self) -> None:
        if not self.report_dir.is_dir():
            raise PackagedEvidenceError(f"report_dir does not exist: {self.report_dir}")
        for label, path in (
            ("result", self.result_path),
            ("job history", self.job_history_path),
            ("UI states", self.states_path),
            ("runtime probe", self.runtime_probe_path),
            ("runtime UI log", self.runtime_log_path),
        ):
            if not _inside(self.report_dir, path):
                raise PackagedEvidenceError(f"{label} path escapes report_dir")
        for group, values in self.spec["inputs"].items():
            if not isinstance(values, list):
                raise PackagedEvidenceError(f"inputs.{group} must be a list")
            for value in values:
                path = Path(value).resolve()
                if group == "product_root":
                    if not path.is_dir():
                        raise PackagedEvidenceError(
                            f"missing TC06 product directory: {path}"
                        )
                elif not path.is_file() or path.stat().st_size <= 0:
                    raise PackagedEvidenceError(f"missing input file: {path}")

    def _resolve_runtime_tool(self, name: str) -> Path | None:
        candidates = (name, f"{name}.exe") if os.name == "nt" else (name,)
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if not resolved:
                continue
            path = Path(resolved).resolve()
            if path.is_file():
                return path
        return None

    def _run_runtime_command(
        self,
        binary: Path,
        arguments: Sequence[str],
    ) -> Dict[str, Any]:
        """Capture one bounded metadata command with raw stdout and stderr."""

        command = [str(binary), *[str(value) for value in arguments]]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUNTIME_PROBE_TIMEOUT_SEC,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {
                "command": command,
                "returncode": int(completed.returncode),
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "timed_out": False,
                "error": "",
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "command": command,
                "returncode": None,
                "stdout": _text(exc.stdout),
                "stderr": _text(exc.stderr),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "timed_out": True,
                "error": (
                    "runtime probe timed out after "
                    f"{RUNTIME_PROBE_TIMEOUT_SEC:.0f}s"
                ),
            }
        except Exception as exc:
            return {
                "command": command,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "timed_out": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _runtime_identity(self, name: str) -> Dict[str, Any]:
        path = self._resolve_runtime_tool(name)
        if path is None:
            self.runtime_evidence_errors.append(
                f"runtime {name} executable not found"
            )
            return {
                "resolved_path": "",
                "exists": False,
                "size": 0,
                "sha256": "",
            }
        try:
            size = path.stat().st_size
            digest = _sha256(path)
        except OSError as exc:
            self.runtime_evidence_errors.append(
                f"runtime {name} identity failed: {exc}"
            )
            size = 0
            digest = ""
        return {
            "resolved_path": str(path),
            "exists": path.is_file(),
            "size": int(size),
            "sha256": digest,
        }

    @staticmethod
    def _runtime_output_has_token(output: str, token: str) -> bool:
        return token in output.split()

    def _runtime_contract_errors(self, payload: Mapping[str, Any]) -> list[str]:
        contract = self.spec.get("runtime_ffmpeg_contract")
        if not isinstance(contract, Mapping):
            return []
        errors: list[str] = []
        ffmpeg = payload.get("ffmpeg")
        ffprobe = payload.get("ffprobe")
        if not isinstance(ffmpeg, Mapping) or not isinstance(ffprobe, Mapping):
            return ["runtime FFmpeg/FFprobe identity is incomplete"]
        for name, tool, field in (
            ("ffmpeg", ffmpeg, "expected_ffmpeg_sha256"),
            ("ffprobe", ffprobe, "expected_ffprobe_sha256"),
        ):
            expected = str(contract.get(field, "") or "").lower()
            actual = str(tool.get("sha256", "") or "").lower()
            if expected and actual != expected:
                errors.append(
                    f"runtime {name} SHA-256 mismatch: expected={expected} actual={actual}"
                )
        expected_environment = contract.get("environment")
        observed_environment = payload.get("environment")
        if isinstance(expected_environment, Mapping):
            if not isinstance(observed_environment, Mapping):
                errors.append("runtime environment capture is missing")
            else:
                for name, expected in expected_environment.items():
                    if observed_environment.get(name) != expected:
                        errors.append(
                            f"runtime environment mismatch {name}: "
                            f"expected={expected!r} "
                            f"actual={observed_environment.get(name)!r}"
                        )
        filters = ffmpeg.get("filters")
        filter_output = ""
        if isinstance(filters, Mapping):
            filter_output = f"{filters.get('stdout', '')}\n{filters.get('stderr', '')}"
        required_filters = contract.get("required_available_filters")
        if isinstance(required_filters, list):
            for name in required_filters:
                if isinstance(name, str) and not self._runtime_output_has_token(
                    filter_output, name
                ):
                    errors.append(f"runtime ffmpeg filter token missing: {name}")
        encoders = ffmpeg.get("encoders")
        encoder_output = ""
        if isinstance(encoders, Mapping):
            encoder_output = f"{encoders.get('stdout', '')}\n{encoders.get('stderr', '')}"
        required_encoders = contract.get("required_encoders")
        if isinstance(required_encoders, list):
            for name in required_encoders:
                if isinstance(name, str) and not self._runtime_output_has_token(
                    encoder_output, name
                ):
                    errors.append(f"runtime ffmpeg encoder token missing: {name}")
        return errors

    def _capture_runtime_probe(self) -> None:
        """Capture the exact FFmpeg tools visible to the packaged process."""

        self.runtime_evidence_errors = []
        ffmpeg = self._runtime_identity("ffmpeg")
        ffprobe = self._runtime_identity("ffprobe")

        ffmpeg_path = (
            Path(ffmpeg["resolved_path"]) if ffmpeg["resolved_path"] else None
        )
        ffprobe_path = (
            Path(ffprobe["resolved_path"]) if ffprobe["resolved_path"] else None
        )
        if ffmpeg_path is not None:
            ffmpeg["version"] = self._run_runtime_command(
                ffmpeg_path, ("-hide_banner", "-version")
            )
            ffmpeg["filters"] = self._run_runtime_command(
                ffmpeg_path, ("-hide_banner", "-filters")
            )
            ffmpeg["encoders"] = self._run_runtime_command(
                ffmpeg_path, ("-hide_banner", "-encoders")
            )
            for label in ("version", "filters", "encoders"):
                command_result = ffmpeg[label]
                if command_result.get("returncode") != 0:
                    detail = (
                        command_result.get("error")
                        or command_result.get("stderr")
                        or "non-zero exit"
                    )
                    self.runtime_evidence_errors.append(
                        f"runtime ffmpeg {label} probe failed: {detail}"
                    )
        if ffprobe_path is not None:
            ffprobe["version"] = self._run_runtime_command(
                ffprobe_path, ("-hide_banner", "-version")
            )
            if ffprobe["version"].get("returncode") != 0:
                detail = (
                    ffprobe["version"].get("error")
                    or ffprobe["version"].get("stderr")
                    or "non-zero exit"
                )
                self.runtime_evidence_errors.append(
                    f"runtime ffprobe version probe failed: {detail}"
                )

        payload: Dict[str, Any] = {
            "schema_version": 1,
            "pair_id": self.pair_id,
            "request_id": str(self.spec["request_id"]),
            "tc_id": self.tc_id,
            "captured_at": _utc_now_iso(),
            "environment": {
                "V3_GREEN_CUDA_FILTERS": os.environ.get(
                    "V3_GREEN_CUDA_FILTERS", ""
                ),
                STRICT_EVIDENCE_ENV: os.environ.get(STRICT_EVIDENCE_ENV, ""),
            },
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
        }
        self.runtime_evidence_errors.extend(
            self._runtime_contract_errors(payload)
        )
        payload["errors"] = list(dict.fromkeys(self.runtime_evidence_errors))
        _atomic_write_json(self.runtime_probe_path, payload)

    def _copy_ui_runtime_log(self) -> tuple[str, str]:
        log_panel = getattr(self.tab, "_log", None)
        source_value = getattr(log_panel, "log_path", None)
        if source_value in (None, ""):
            return "", "selected tab has no UI runtime log path"
        try:
            source = Path(source_value).resolve()
        except (OSError, TypeError, ValueError) as exc:
            return "", f"invalid UI runtime log path: {exc}"
        if not source.is_file() or source.stat().st_size <= 0:
            return "", f"UI runtime log is missing or empty: {source}"
        destination = self.runtime_log_path
        if not _inside(self.report_dir, destination):
            return "", "runtime UI log destination escapes report_dir"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source != destination.resolve():
                tmp = destination.with_name(
                    f".{destination.name}.{os.getpid()}.tmp"
                )
                try:
                    shutil.copyfile(source, tmp)
                    if not tmp.is_file() or tmp.stat().st_size <= 0:
                        raise PackagedEvidenceError(
                            "copied UI runtime log is empty"
                        )
                    os.replace(tmp, destination)
                finally:
                    try:
                        if tmp.exists():
                            tmp.unlink()
                    except OSError:
                        pass
            if not destination.is_file() or destination.stat().st_size <= 0:
                return "", (
                    "copied UI runtime log is missing or empty: "
                    f"{destination}"
                )
        except Exception as exc:
            return "", f"UI runtime log copy failed: {exc}"
        return str(destination), ""

    def _runtime_result_fields(self) -> Dict[str, Any]:
        errors = list(self.runtime_evidence_errors)
        probe_path = ""
        probe_sha256 = ""
        try:
            if (
                self.runtime_probe_path.is_file()
                and self.runtime_probe_path.stat().st_size > 0
            ):
                probe_path = str(self.runtime_probe_path)
                probe_sha256 = _sha256(self.runtime_probe_path)
            else:
                errors.append(
                    f"runtime probe is missing or empty: {self.runtime_probe_path}"
                )
        except OSError as exc:
            errors.append(f"runtime probe inspection failed: {exc}")

        runtime_log_path, runtime_log_error = self._copy_ui_runtime_log()
        runtime_log_sha256 = ""
        if runtime_log_error:
            errors.append(runtime_log_error)
        elif runtime_log_path:
            try:
                runtime_log_sha256 = _sha256(Path(runtime_log_path))
            except OSError as exc:
                errors.append(f"runtime UI log hash failed: {exc}")

        unique_errors = list(dict.fromkeys(errors))
        return {
            "runtime_probe_path": probe_path,
            "runtime_probe_sha256": probe_sha256,
            "runtime_log_path": runtime_log_path,
            "runtime_log_sha256": runtime_log_sha256,
            "runtime_errors": unique_errors,
            "runtime_evidence_errors": unique_errors,
        }

    def _write_result_payload(self, payload: Mapping[str, Any]) -> None:
        value = dict(payload)
        if self.runtime_capture_required:
            value.update(self._runtime_result_fields())
        _atomic_write_json(self.result_path, value)

    def _apply_inputs(self) -> None:
        for group, values in self.spec["inputs"].items():
            zone = self.tab._zones.get(group)
            if zone is None:
                if values:
                    raise PackagedEvidenceError(
                        f"{self.tc_id} has no input zone {group!r}"
                    )
                continue
            zone.set_files([str(Path(value).resolve()) for value in values])

    def _apply_settings(self) -> None:
        requested: Dict[str, Dict[str, Any]] = {}
        observed: Dict[str, Dict[str, Any]] = {}
        mismatches: list[str] = []
        for group, values in self.spec["settings"].items():
            if not isinstance(values, dict):
                raise PackagedEvidenceError(f"settings.{group} must be an object")
            requested[group] = dict(values)
            panel = getattr(self.tab, f"_{group}_settings", None)
            if panel is None:
                if values:
                    raise PackagedEvidenceError(
                        f"{self.tc_id} has no settings panel {group!r}"
                    )
                continue
            panel.set_values(values)
            observed[group] = {}
            for key, expected in values.items():
                actual = panel.get_value(key)
                observed[group][key] = actual
                expected_json = json.dumps(
                    expected,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                actual_json = json.dumps(
                    actual,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                if actual_json != expected_json:
                    mismatches.append(
                        f"settings.{group}.{key}: "
                        f"requested={expected!r}, observed={actual!r}"
                    )
        self.ui_assertions["settings_application"] = {
            "tc_id": self.tc_id,
            "requested": requested,
            "observed": observed,
            "errors": mismatches,
            "passed": not mismatches and requested == observed,
        }

    def _prepare_encoder_evidence(self) -> None:
        expectation = str(self.spec.get("encoder_expectation", "") or "").strip()
        force_failure = bool(self.spec.get("force_hardware_failure_once", False))
        audit_only = self.tc06_scenario in _TC06_ENGINE_AUDIT_SCENARIOS
        if not expectation and not force_failure and not audit_only:
            return
        from core.encoder_recovery import (
            command_video_encoder,
            is_hardware_encoder,
            record_encoder_attempt,
            start_encoder_audit,
        )

        start_encoder_audit(self.pair_id)
        self._encoder_audit_started = True
        if not force_failure:
            return

        from core.ffmpeg_runner import FfmpegProgress, FfmpegResult, FfmpegRunner

        original_run = FfmpegRunner.run
        self._original_ffmpeg_run = original_run
        controller = self

        def run_with_one_hardware_fault(runner: Any, cmd: Any, *args: Any, **kwargs: Any):
            encoder = command_video_encoder(cmd)
            if (
                not controller._encoder_fault_injected
                and is_hardware_encoder(encoder)
            ):
                controller._encoder_fault_injected = True
                partial_path = Path(str(cmd[-1])).resolve()
                partial_path.parent.mkdir(parents=True, exist_ok=True)
                partial_payload = b"STRICT_EVIDENCE_PARTIAL_BEFORE_GPU_FAILURE"
                partial_path.write_bytes(partial_payload)
                progress = FfmpegProgress(
                    pct=37.0,
                    out_time_us=370_000,
                    elapsed_sec=0.37,
                )
                progress_callback = kwargs.get("on_progress")
                if callable(progress_callback):
                    progress_callback(progress)
                injected = FfmpegResult(
                    success=False,
                    returncode=86,
                    error=(
                        "strict evidence injected one hardware encoder failure "
                        "after partial output and progress before CPU recovery"
                    ),
                )
                record_encoder_attempt(
                    cmd,
                    injected,
                    injected=True,
                    details={
                        "failure_stage": "after_partial_and_progress",
                        "partial_bytes_before_failure": len(partial_payload),
                        "progress_pct_before_failure": progress.pct,
                    },
                )
                return injected
            return original_run(runner, cmd, *args, **kwargs)

        FfmpegRunner.run = run_with_one_hardware_fault

    def _restore_ffmpeg_runner(self) -> None:
        if self._original_ffmpeg_run is not None:
            from core.ffmpeg_runner import FfmpegRunner

            FfmpegRunner.run = self._original_ffmpeg_run
            self._original_ffmpeg_run = None
        if self._encoder_audit_started:
            try:
                from core.encoder_recovery import end_encoder_audit

                end_encoder_audit(self.pair_id)
            finally:
                self._encoder_audit_started = False

    def _finalize_encoder_evidence(self) -> None:
        expectation = str(self.spec.get("encoder_expectation", "") or "").strip()
        if not expectation:
            if self._encoder_audit_started:
                from core.encoder_recovery import snapshot_encoder_attempts

                self._encoder_attempts_snapshot = snapshot_encoder_attempts(
                    self.pair_id
                )
                attempts_correlated = bool(
                    not self._encoder_attempts_snapshot
                    or all(
                        item.get("session_id") == self.pair_id
                        for item in self._encoder_attempts_snapshot
                    )
                )
                self.ui_assertions["encoder_recovery"] = {
                    "expectation": "audit_only",
                    "audit_session_id": self.pair_id,
                    "attempts": list(self._encoder_attempts_snapshot),
                    "attempts_correlated": attempts_correlated,
                    "passed": attempts_correlated,
                }
            self._restore_ffmpeg_runner()
            return
        from core.encoder_recovery import (
            evaluate_encoder_expectation,
            snapshot_encoder_attempts,
        )

        self._encoder_attempts_snapshot = snapshot_encoder_attempts(self.pair_id)
        assertion = evaluate_encoder_expectation(
            expectation,
            self._encoder_attempts_snapshot,
        )
        assertion["audit_session_id"] = self.pair_id
        assertion["attempts_correlated"] = bool(
            assertion["attempts"]
            and all(
                item.get("session_id") == self.pair_id
                for item in assertion["attempts"]
            )
        )
        assertion["passed"] = bool(
            assertion["passed"] and assertion["attempts_correlated"]
        )
        assertion["forced_hardware_failure_requested"] = bool(
            self.spec.get("force_hardware_failure_once", False)
        )
        assertion["forced_hardware_failure_injected"] = self._encoder_fault_injected
        if assertion["forced_hardware_failure_requested"]:
            assertion["passed"] = bool(
                assertion["passed"] and self._encoder_fault_injected
            )
            injected_paths = [
                Path(str(item.get("output_path", "")))
                for item in assertion["attempts"]
                if item.get("injected") is True and item.get("output_path")
            ]
            assertion["injected_partials_removed"] = bool(
                injected_paths
                and all(not path.exists() for path in injected_paths)
            )
            assertion["passed"] = bool(
                assertion["passed"] and assertion["injected_partials_removed"]
            )
        self.ui_assertions["encoder_recovery"] = assertion
        self._restore_ffmpeg_runner()

    def _verify_chroma_defaults_and_reset(self) -> None:
        """Machine-check the live packaged Chroma panel and its Reset button.

        This check runs before evidence settings are applied, so it proves the
        values a user sees on a fresh packaged tab.  Reset is exercised through
        the real button command with a deterministic affirmative confirmation;
        direct ``reset_to_defaults()`` calls would not prove that the UI control
        is wired.
        """

        if self.tc_id not in {"TC01", "TC02", "TC03", "TC04"}:
            raise PackagedEvidenceError(
                "Chroma defaults/Reset assertion is only valid for TC01-TC04"
            )
        panel = getattr(self.tab, "_video_settings", None)
        if panel is None:
            raise PackagedEvidenceError(f"{self.tc_id} has no Chroma settings panel")

        expected_value = self.spec.get("expected_chroma_defaults")
        if not isinstance(expected_value, dict) or not expected_value:
            raise PackagedEvidenceError(
                "expected_chroma_defaults must be supplied by the external runner"
            )
        expected = dict(expected_value)
        if set(expected) != REQUIRED_CHROMA_DEFAULT_KEYS:
            raise PackagedEvidenceError(
                "expected_chroma_defaults must contain exactly the shared "
                "TC01-TC04 Chroma fields"
            )
        initial = {key: panel.get_value(key) for key in expected}
        initial_matches = initial == expected
        mutations = {
            "key_color": "#0000FF",
            "similarity": 0.77,
            "blend": 0.21,
            "despill": 0.68,
        }
        panel.set_values(mutations)
        mutated = {key: panel.get_value(key) for key in mutations}
        mutation_applied = mutated == mutations

        reset_button = getattr(panel, "_reset_button", None)
        if reset_button is None or not hasattr(reset_button, "invoke"):
            raise PackagedEvidenceError(
                f"{self.tc_id} Chroma panel has no invokable Reset defaults button"
            )

        from ui.components import settings_panel as settings_panel_module

        original_confirm = settings_panel_module.messagebox.askyesno
        try:
            settings_panel_module.messagebox.askyesno = lambda *_a, **_kw: True
            reset_button.invoke()
            self.root.update_idletasks()
        finally:
            settings_panel_module.messagebox.askyesno = original_confirm

        after_reset = {key: panel.get_value(key) for key in expected}
        reset_matches = after_reset == expected
        assertion = {
            "tc_id": self.tc_id,
            "panel": "video",
            "expected": expected,
            "initial": initial,
            "initial_matches": initial_matches,
            "mutations": mutations,
            "mutated": mutated,
            "mutation_applied": mutation_applied,
            "reset_button_text": str(reset_button.cget("text")),
            "reset_invoked_through_button": True,
            "after_reset": after_reset,
            "reset_matches": reset_matches,
            "passed": bool(initial_matches and mutation_applied and reset_matches),
        }
        self.ui_assertions["chroma_defaults_reset"] = assertion

    def _scroll(self, fraction: float) -> None:
        canvas = getattr(self.tab, "_canvas", None)
        if canvas is not None:
            canvas.yview_moveto(max(0.0, min(1.0, fraction)))
            self.root.update_idletasks()

    def _live_input_zone_state(self) -> Dict[str, Any]:
        """Read the files currently owned by each live DropZone.

        Evidence must describe the UI at capture time.  In particular, the
        spec is not a substitute for the DropZone state: step 01 is captured
        before inputs are applied, while step 02 must prove the files that the
        UI actually accepted.  An unreadable zone is represented as unknown,
        never as a made-up zero count.
        """

        zones = getattr(self.tab, "_zones", None)
        if not isinstance(zones, Mapping):
            return {
                "available": False,
                "complete": False,
                "total_count": None,
                "zones": {},
                "read_errors": ["tab._zones is unavailable"],
            }

        observed: Dict[str, Any] = {}
        errors: list[str] = []
        total = 0
        for raw_role, zone in sorted(zones.items(), key=lambda item: str(item[0])):
            role = str(raw_role)
            getter = getattr(zone, "get_files", None)
            if not callable(getter):
                observed[role] = {"count": None, "basenames": []}
                errors.append(f"input zone {role!r} has no get_files()")
                continue
            try:
                values = getter()
            except Exception as exc:
                observed[role] = {"count": None, "basenames": []}
                errors.append(
                    f"input zone {role!r} read failed: {type(exc).__name__}: {exc}"
                )
                continue
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                observed[role] = {"count": None, "basenames": []}
                errors.append(f"input zone {role!r} returned a non-sequence")
                continue

            basenames: list[str] = []
            invalid_values = 0
            for value in values:
                try:
                    raw_path = os.fspath(value)
                    path_text = _text(raw_path)
                except (TypeError, ValueError):
                    invalid_values += 1
                    continue
                if not path_text:
                    invalid_values += 1
                    continue
                basenames.append(Path(path_text).name)
            if invalid_values:
                observed[role] = {
                    "count": None,
                    "basenames": basenames,
                    "invalid_value_count": invalid_values,
                }
                errors.append(
                    f"input zone {role!r} contains {invalid_values} invalid path value(s)"
                )
                continue

            count = len(basenames)
            total += count
            observed[role] = {"count": count, "basenames": basenames}

        return {
            "available": True,
            "complete": not errors,
            "total_count": total if not errors else None,
            "zones": observed,
            "read_errors": errors,
        }

    @staticmethod
    def _path_list_state(values: Any) -> Dict[str, Any]:
        """Return count/basenames for one observed path sequence without guessing."""

        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            return {"available": False, "count": None, "basenames": []}
        basenames: list[str] = []
        for value in values:
            try:
                path_text = _text(os.fspath(value))
            except (TypeError, ValueError):
                return {"available": False, "count": None, "basenames": []}
            if not path_text:
                return {"available": False, "count": None, "basenames": []}
            basenames.append(Path(path_text).name)
        return {"available": True, "count": len(basenames), "basenames": basenames}

    def _live_log_state(self) -> Dict[str, Any]:
        """Describe the selected tab's current append-only UI log."""

        log_panel = getattr(self.tab, "_log", None)
        raw_path = getattr(log_panel, "log_path", None)
        if raw_path in (None, ""):
            return {
                "available": False,
                "path": "",
                "bytes": None,
                "last_nonempty_line": "",
                "error": "selected tab has no UI log path",
            }
        try:
            path = Path(raw_path).resolve()
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode("utf-8", errors="replace")
            lines = [line.strip() for line in tail.splitlines() if line.strip()]
            return {
                "available": True,
                "path": str(path),
                "bytes": int(size),
                "last_nonempty_line": lines[-1] if lines else "",
                "error": "",
            }
        except (OSError, TypeError, ValueError) as exc:
            return {
                "available": False,
                "path": _text(raw_path),
                "bytes": None,
                "last_nonempty_line": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _tc02_audio_policy_state(
        self,
        input_state: Mapping[str, Any],
        result: Any,
    ) -> Dict[str, Any]:
        zones = input_state.get("zones")
        audio = zones.get("audio") if isinstance(zones, Mapping) else None
        count = audio.get("count") if isinstance(audio, Mapping) else None
        basenames = (
            list(audio.get("basenames", []))
            if isinstance(audio, Mapping) and isinstance(audio.get("basenames"), list)
            else []
        )
        if count is None:
            return {
                "status": "UNKNOWN",
                "ready": False,
                "selected_audio_count": None,
                "selected_audio_basenames": basenames,
                "policy": "unresolved_live_audio_zone",
            }
        if count == 0:
            return {
                "status": "NOT_APPLICABLE",
                "ready": True,
                "selected_audio_count": 0,
                "selected_audio_basenames": [],
                "policy": "preserve_product_audio_when_present",
            }
        success = bool(result is not None and getattr(result, "is_success", False))
        return {
            "status": "READY" if success else "ERROR",
            "ready": success,
            "selected_audio_count": int(count),
            "selected_audio_basenames": basenames,
            "policy": "external_audio_is_duration_master",
        }

    def _terminal_evidence_state(
        self,
        *,
        input_state: Mapping[str, Any],
        result: Any,
        record: Any,
    ) -> Dict[str, Any]:
        result_outputs = self._path_list_state(
            getattr(result, "outputs", None) if result is not None else None
        )
        record_outputs = self._path_list_state(
            record.get("outputs") if isinstance(record, Mapping) else None
        )
        gallery = self._path_list_state(getattr(self.tab, "_gallery_paths", None))
        terminal = {
            "pipeline_result_present": result is not None,
            "pipeline_status": (
                result.status.value
                if result is not None and getattr(result, "status", None) is not None
                else ""
            ),
            "pipeline_success": bool(
                result is not None and getattr(result, "is_success", False)
            ),
            "output_count": result_outputs["count"],
            "output_basenames": result_outputs["basenames"],
            "record_output_count": record_outputs["count"],
            "record_output_basenames": record_outputs["basenames"],
            "gallery_state": gallery,
            "log_state": self._live_log_state(),
        }
        if self.tc_id == "TC02":
            terminal["audio_policy"] = self._tc02_audio_policy_state(
                input_state,
                result,
            )
        return terminal

    def _state_payload(self, step: str) -> Dict[str, Any]:
        result = getattr(self.tab, "_last_pipeline_result", None)
        record = getattr(self.tab, "_last_job_record", None)
        preflight = getattr(self.tab, "_preflight_last_result", None)
        input_state = self._live_input_zone_state()
        progress = getattr(self.tab, "_progress", None)
        progress_value = None
        try:
            progress_value = float(progress._bar["value"]) if progress is not None else None
        except Exception:
            progress_value = None
        payload = {
            "step": step,
            "evidence_phase": {
                "01_open_page": "OPEN_PAGE",
                "02_input_ready": "INPUTS_APPLIED",
                "03_click_generate": "GENERATE_INVOKED",
                "04_result_state": "RESULT_OBSERVED",
                "05_audio_ready_or_error": "TERMINAL_EVIDENCE",
            }.get(step, "UNKNOWN"),
            "tc_id": self.tc_id,
            "scenario": self.tc06_scenario,
            "pair_id": self.pair_id,
            "window_title": str(self.root.title()),
            "window_geometry": str(self.root.geometry()),
            "worker_busy": bool(self.tab.worker.is_busy()),
            "progress_value": progress_value,
            "render_button_state": str(self.tab._render_btn.cget("state")),
            "job_id": record.get("id") if isinstance(record, dict) else "",
            "pipeline_status": (
                result.status.value
                if result is not None and getattr(result, "status", None) is not None
                else ""
            ),
            "pipeline_success": bool(
                result is not None and getattr(result, "is_success", False)
            ),
            "preflight": (
                preflight.to_dict()
                if preflight is not None and hasattr(preflight, "to_dict")
                else None
            ),
            "input_zones": input_state,
            "ui_assertions": self.ui_assertions,
            "captured_at_monotonic_sec": round(time.monotonic() - self.started_at, 3),
        }
        if step == "05_audio_ready_or_error":
            payload["terminal_evidence"] = self._terminal_evidence_state(
                input_state=input_state,
                result=result,
                record=record,
            )
        return payload

    @staticmethod
    def _capture_issue(
        image: Any,
        *,
        expected_size: tuple[int, int] | None = None,
    ) -> str:
        """Return why a screenshot is unusable, or an empty string when valid."""

        width = int(getattr(image, "width", 0) or 0)
        height = int(getattr(image, "height", 0) or 0)
        if width <= 0 or height <= 0:
            return f"invalid size {width}x{height}"
        if expected_size is not None and (width, height) != expected_size:
            return (
                f"size mismatch expected {expected_size[0]}x{expected_size[1]}, "
                f"got {width}x{height}"
            )
        colors = image.convert("RGB").getcolors(maxcolors=2)
        if colors is not None and len(colors) <= 1:
            return "uniform image"
        return ""

    def _recover_capture_surface(self) -> None:
        """Ask Tk/DWM to repaint before another bounded capture round."""

        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.root.update_idletasks()
        self.root.update()
        # Give DWM one compositor tick.  This runs only after both capture
        # methods returned unusable evidence, never in the normal UI path.
        time.sleep(0.05)
        self.root.update()

    def _capture(self, step: str) -> str:
        if step not in REQUIRED_STEPS:
            raise PackagedEvidenceError(f"unsupported screenshot step: {step}")
        self.root.update_idletasks()
        self.root.update()
        left = int(self.root.winfo_rootx())
        top = int(self.root.winfo_rooty())
        width = int(self.root.winfo_width())
        height = int(self.root.winfo_height())
        if width <= 0 or height <= 0:
            raise PackagedEvidenceError(
                f"Tk returned invalid capture size: {width}x{height}"
            )
        destination = (
            self.report_dir
            / "screenshots"
            / f"{self.pair_id}__actual_tk__{step}.png"
        )
        if not _inside(self.report_dir, destination):
            raise PackagedEvidenceError("screenshot path escapes report_dir")
        if destination.exists():
            raise PackagedEvidenceError(f"screenshot already exists: {destination}")
        from PIL import ImageGrab

        image = None
        capture_method = ""
        capture_attempt_count = 0
        failures: list[str] = []
        for capture_round in range(3):
            capture_attempt_count = capture_round + 1
            attempts = (
                (
                    "desktop_bbox_imagegrab",
                    lambda: ImageGrab.grab(
                        bbox=(left, top, left + width, top + height),
                        all_screens=True,
                    ),
                    (width, height),
                ),
                (
                    "tk_hwnd_imagegrab",
                    lambda: ImageGrab.grab(window=int(self.root.winfo_id())),
                    (width, height),
                ),
            )
            for method, grab, expected_size in attempts:
                try:
                    candidate = grab()
                    issue = self._capture_issue(
                        candidate,
                        expected_size=expected_size,
                    )
                except Exception as exc:
                    failures.append(
                        f"round={capture_round + 1} method={method}: {exc}"
                    )
                    continue
                if issue:
                    failures.append(
                        f"round={capture_round + 1} method={method}: {issue}"
                    )
                    continue
                image = candidate
                capture_method = method
                break
            if image is not None:
                break
            if capture_round < 2:
                self._recover_capture_surface()
        if image is None:
            raise PackagedEvidenceError(
                "actual Tk capture failed after desktop/HWND recovery: "
                + "; ".join(failures)
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG")
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise PackagedEvidenceError(f"empty screenshot: {destination}")
        relative = destination.relative_to(self.report_dir).as_posix()
        self.screenshots.append(relative)
        self.states[step] = {
            **self._state_payload(step),
            "screenshot": relative,
            "screenshot_sha256": _sha256(destination),
            "screenshot_width": image.width,
            "screenshot_height": image.height,
            "capture_method": capture_method,
            "capture_attempt_count": capture_attempt_count,
            "transient_capture_failures": failures,
        }
        _atomic_write_json(self.states_path, self.states)
        return relative

    def _poll_render(self) -> None:
        if self.finished:
            return
        elapsed = time.monotonic() - self.started_at
        if elapsed > self.timeout_sec:
            try:
                self.tab.worker.cancel()
            except Exception:
                pass
            self._fail(
                PackagedEvidenceError(
                    f"{self.tc_id} packaged render timed out after {elapsed:.1f}s"
                )
            )
            return
        self._service_tc06_stop_request()
        if self.tab.worker.is_busy():
            self._poll_after_id = self.root.after(100, self._poll_render)
            return
        result = getattr(self.tab, "_last_pipeline_result", None)
        record = getattr(self.tab, "_last_job_record", None)
        preflight = getattr(self.tab, "_preflight_last_result", None)
        if (
            result is None
            and not isinstance(record, dict)
            and preflight is not None
            and getattr(preflight, "ok", True) is False
        ):
            self._finalize_preflight_rejection(preflight)
            return
        if result is None or not isinstance(record, dict):
            self._poll_after_id = self.root.after(100, self._poll_render)
            return
        try:
            # Persist functional truth before screenshot transport.  A later
            # DWM/ImageGrab failure must still leave the completed job/result
            # available for RCA, while the pair correctly remains incomplete.
            _atomic_write_json(self.job_history_path, [record])
            self._finalize_encoder_evidence()
            if self.tc06_scenario in {
                "layout_drift",
                "stop_chroma",
                "resume_chroma",
                "inject_folder",
            }:
                self._finalize_tc06_terminal_scenario(
                    result,
                    record,
                    preflight,
                )
                return
            self._scroll(float(self.spec.get("result_scroll_fraction", 0.55)))
            self._capture("04_result_state")
            self._scroll(1.0)
            self._capture("05_audio_ready_or_error")
            success = bool(getattr(result, "is_success", False))
            payload = {
                "success": success,
                "tc_id": self.tc_id,
                "pair_id": self.pair_id,
                "request_id": self.spec["request_id"],
                "job_id": record.get("id", ""),
                "job_history_path": str(self.job_history_path),
                "screenshots": list(self.screenshots),
                "states_path": str(self.states_path),
                "elapsed_ms": round(
                    (time.monotonic() - self.started_at) * 1000.0, 3
                ),
                "window_title": str(self.root.title()),
                "ui_assertions": self.ui_assertions,
                "pipeline_result": result.to_dict(),
                "error": "" if success else "; ".join(result.all_errors[:5]),
            }
            self._write_result_payload(payload)
            self.finished = True
            self._close_app()
        except Exception as exc:
            self._fail(exc)

    def _preflight_failure_error(self, preflight: Any) -> str:
        failed = list(getattr(preflight, "failed_items", []) or [])
        if not failed:
            return "preflight rejected the render request"
        parts = []
        for item in failed[:5]:
            label = str(getattr(item, "label", "") or "").strip()
            detail = str(getattr(item, "detail", "") or "").strip()
            parts.append(": ".join(value for value in (label, detail) if value))
        return "; ".join(parts) or "preflight rejected the render request"

    def _finalize_ui_validation_rejection(
        self,
        rejection: Dict[str, Any] | None,
    ) -> None:
        """Persist a complete expected TC06 rejection before preflight."""

        try:
            if not isinstance(rejection, dict) or rejection.get("passed") is not True:
                raise PackagedEvidenceError(
                    "TC06 UI validation rejection assertion is missing or failed"
                )
            self._scroll(float(self.spec.get("result_scroll_fraction", 0.55)))
            self._capture("04_result_state")
            self._scroll(1.0)
            self._capture("05_audio_ready_or_error")
            _atomic_write_json(self.job_history_path, [])
            warning_messages = [
                str(item.get("message", ""))
                for item in rejection.get("dialogs", [])
                if isinstance(item, dict) and item.get("kind") == "warning"
            ]
            payload = {
                "success": False,
                "scenario": "missing_role",
                "scenario_verdict": "PASS",
                "tc_id": self.tc_id,
                "pair_id": self.pair_id,
                "request_id": self.spec["request_id"],
                "job_id": "",
                "job_history_path": str(self.job_history_path),
                "screenshots": list(self.screenshots),
                "states_path": str(self.states_path),
                "elapsed_ms": round(
                    (time.monotonic() - self.started_at) * 1000.0,
                    3,
                ),
                "window_title": str(self.root.title()),
                "ui_assertions": self.ui_assertions,
                "pipeline_result": None,
                "preflight_result": None,
                "rejection_stage": "ui_validation",
                "worker_started": False,
                "preflight_started": False,
                "engine_started": False,
                "outputs": [],
                "output_count": 0,
                "artifact_outputs": [],
                "artifact_count": 0,
                "scenario_details": rejection,
                "error": "; ".join(warning_messages)
                or "TC06 missing-role UI validation rejected the render request",
            }
            self._write_result_payload(payload)
            self.finished = True
            self._close_app()
        except Exception as exc:
            self._fail(exc)

    def _finalize_preflight_rejection(self, preflight: Any) -> None:
        """Capture and close a structured UI-side input rejection.

        Preflight failures intentionally do not start a Worker and therefore
        cannot produce a PipelineResult/job-history row.  In explicit strict
        evidence mode that is still a complete, truth-bearing FAIL result,
        not a reason to poll until timeout.
        """

        try:
            self._scroll(float(self.spec.get("result_scroll_fraction", 0.55)))
            self._capture("04_result_state")
            self._scroll(1.0)
            self._capture("05_audio_ready_or_error")
            _atomic_write_json(self.job_history_path, [])
            payload = {
                "success": False,
                "tc_id": self.tc_id,
                "pair_id": self.pair_id,
                "request_id": self.spec["request_id"],
                "job_id": "",
                "job_history_path": str(self.job_history_path),
                "screenshots": list(self.screenshots),
                "states_path": str(self.states_path),
                "elapsed_ms": round(
                    (time.monotonic() - self.started_at) * 1000.0, 3
                ),
                "window_title": str(self.root.title()),
                "ui_assertions": self.ui_assertions,
                "pipeline_result": None,
                "preflight_result": (
                    preflight.to_dict()
                    if hasattr(preflight, "to_dict")
                    else None
                ),
                "rejection_stage": "preflight",
                "error": self._preflight_failure_error(preflight),
            }
            self._write_result_payload(payload)
            self.finished = True
            self._close_app()
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc: BaseException) -> None:
        if self.finished:
            return
        self.finished = True
        result = getattr(self.tab, "_last_pipeline_result", None)
        record = getattr(self.tab, "_last_job_record", None)
        try:
            pipeline_result = (
                result.to_dict()
                if result is not None and hasattr(result, "to_dict")
                else None
            )
        except Exception:
            pipeline_result = None
        payload = {
            "success": False,
            "tc_id": self.tc_id,
            "pair_id": self.pair_id,
            "request_id": self.spec.get("request_id", ""),
            "job_id": record.get("id", "") if isinstance(record, dict) else "",
            "job_history_path": str(self.job_history_path),
            "screenshots": list(self.screenshots),
            "states_path": str(self.states_path),
            "elapsed_ms": round((time.monotonic() - self.started_at) * 1000.0, 3),
            "window_title": str(self.root.title()),
            "ui_assertions": self.ui_assertions,
            "pipeline_result": pipeline_result,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        if self.tc06_scenario != "render":
            payload["scenario"] = self.tc06_scenario
            payload["scenario_verdict"] = "FAIL"
        try:
            self._write_result_payload(payload)
        finally:
            self._stop_acknowledged.set()
            self._restore_ffmpeg_runner()
            self._restore_runtime_hooks()
            self._close_app()

    def _close_app(self) -> None:
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass
        close = getattr(self.app, "_on_close_window", None)
        self.root.after(250, close if callable(close) else self.root.destroy)


__all__ = [
    "PackagedEvidenceController",
    "PackagedEvidenceError",
    "REQUIRED_STEPS",
    "REQUIRED_CHROMA_DEFAULT_KEYS",
    "TAB_MAP",
    "TC06_SCENARIOS",
]
