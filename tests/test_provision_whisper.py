"""Unit tests for the pure logic in provision_whisper.py.

Covers GPU classification, CUDA version parsing, backend choice, plan
building, and rollback bookkeeping. No hardware, network, or winget — the
side-effecting provisioning steps are verified manually (see the plan's
manual-verification checklist).

Run:
    py -m pytest tests/test_provision_whisper.py
    # or, with no pytest installed:
    py tests/test_provision_whisper.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import provision_whisper as pw  # noqa: E402


# --- parse_video_controllers ----------------------------------------------

def test_parse_video_controllers_strips_and_drops_blanks():
    text = "NVIDIA GeForce RTX 4070\r\n\r\nIntel(R) UHD Graphics\r\n"
    assert pw.parse_video_controllers(text) == [
        "NVIDIA GeForce RTX 4070",
        "Intel(R) UHD Graphics",
    ]


def test_parse_video_controllers_empty():
    assert pw.parse_video_controllers("") == []


# --- classify_vendor -------------------------------------------------------

def test_classify_nvidia():
    assert pw.classify_vendor(["NVIDIA GeForce RTX 4070"]) == "nvidia"


def test_classify_amd():
    assert pw.classify_vendor(["AMD Radeon RX 6800"]) == "amd"


def test_classify_intel():
    assert pw.classify_vendor(["Intel(R) UHD Graphics 770"]) == "intel"


def test_classify_discrete_beats_integrated():
    # Typical laptop: Intel iGPU + NVIDIA dGPU -> pick NVIDIA.
    assert pw.classify_vendor(["Intel(R) UHD Graphics", "NVIDIA GeForce RTX 3050"]) == "nvidia"


def test_classify_none():
    assert pw.classify_vendor([]) == "none"
    assert pw.classify_vendor(["Microsoft Basic Display Adapter"]) == "none"


# --- parse_cuda_version / cuda_zip_url -------------------------------------

def test_parse_cuda_version_found():
    assert pw.parse_cuda_version("Driver Version: 552.22   CUDA Version: 12.4 ") == "12.4"


def test_parse_cuda_version_missing():
    assert pw.parse_cuda_version("no cuda here") is None
    assert pw.parse_cuda_version("") is None


def test_cuda_zip_url_12x():
    assert "cublas-12.4.0" in pw.cuda_zip_url("12.4")


def test_cuda_zip_url_11x():
    assert "cublas-11.8.0" in pw.cuda_zip_url("11.8")


def test_cuda_zip_url_unknown_defaults_to_12():
    assert "cublas-12.4.0" in pw.cuda_zip_url(None)


# --- choose_backend --------------------------------------------------------

def test_choose_backend_auto_nvidia():
    assert pw.choose_backend("nvidia", "auto") == "cuda"


def test_choose_backend_auto_amd_intel():
    assert pw.choose_backend("amd", "auto") == "vulkan"
    assert pw.choose_backend("intel", "auto") == "vulkan"


def test_choose_backend_auto_none():
    assert pw.choose_backend("none", "auto") == "cpu"


def test_choose_backend_explicit_overrides():
    assert pw.choose_backend("nvidia", "cpu") == "cpu"
    assert pw.choose_backend("none", "vulkan") == "vulkan"


# --- build_plan ------------------------------------------------------------

def _no_probes():
    return {"whisper": False, "model": False, "espeak": False,
            "vsbuildtools": False, "vulkan_sdk": False}


def test_plan_cpu_has_whisper_model_espeak():
    gpu = pw.GpuInfo(vendor="none")
    keys = [s.key for s in pw.build_plan("cpu", gpu, _no_probes())]
    assert keys == ["whisper-cpu", "model", "espeak"]


def test_plan_cuda_no_system_installs():
    gpu = pw.GpuInfo(vendor="nvidia", cuda_version="12.4")
    keys = [s.key for s in pw.build_plan("cuda", gpu, _no_probes())]
    assert keys == ["whisper-cuda", "model", "espeak"]


def test_plan_vulkan_includes_build_tools_and_sdk():
    gpu = pw.GpuInfo(vendor="amd")
    keys = [s.key for s in pw.build_plan("vulkan", gpu, _no_probes())]
    assert keys == ["vs-build-tools", "vulkan-sdk", "whisper-vulkan", "model", "espeak"]


def test_plan_skips_already_installed():
    gpu = pw.GpuInfo(vendor="amd")
    probes = _no_probes()
    probes.update({"vsbuildtools": True, "vulkan_sdk": True, "model": True})
    plan = pw.build_plan("vulkan", gpu, probes)
    by_key = {s.key: s for s in plan}
    assert by_key["vs-build-tools"].skip is True
    assert by_key["vulkan-sdk"].skip is True
    assert by_key["model"].skip is True
    assert by_key["whisper-vulkan"].skip is False
    assert by_key["espeak"].skip is False


def test_plan_to_text_marks_skip_and_do():
    gpu = pw.GpuInfo(vendor="none")
    probes = _no_probes()
    probes["model"] = True
    text = pw.plan_to_text(pw.build_plan("cpu", gpu, probes))
    assert "[SKIP]" in text
    assert "[DO  ]" in text
    assert "Total to do" in text


def test_plan_to_json_roundtrips():
    import json
    gpu = pw.GpuInfo(vendor="none")
    data = json.loads(pw.plan_to_json(pw.build_plan("cpu", gpu, _no_probes())))
    assert data[0]["key"] == "whisper-cpu"
    assert "size_mb" in data[0]


# --- Rollbacker ------------------------------------------------------------

def test_rollback_removes_tracked_and_keeps_untracked():
    import tempfile, shutil as _sh
    base = Path(tempfile.mkdtemp())
    try:
        pre_existing = base / "keep.txt"
        pre_existing.write_text("keep", encoding="utf-8")
        new_file = base / "new.zip"
        new_dir = base / "clone"
        rb = pw.Rollbacker()
        new_file.write_text("data", encoding="utf-8")
        rb.track(new_file)
        new_dir.mkdir()
        (new_dir / "x").write_text("y", encoding="utf-8")
        rb.track(new_dir)
        rb.rollback()
        assert not new_file.exists()
        assert not new_dir.exists()
        assert pre_existing.exists()  # untouched
    finally:
        _sh.rmtree(base, ignore_errors=True)


def test_rollback_restores_backup():
    import tempfile, shutil as _sh
    base = Path(tempfile.mkdtemp())
    try:
        original = base / "Release"
        original.mkdir()
        (original / "whisper-server.exe").write_text("OLD", encoding="utf-8")
        backup = base / "Release.bak"
        original.rename(backup)            # simulate "move old aside"
        rb = pw.Rollbacker()
        rb.track_backup(original, backup)
        original.mkdir()                   # simulate new (failed) install
        (original / "whisper-server.exe").write_text("NEW-BROKEN", encoding="utf-8")
        rb.track(original)
        rb.rollback()
        assert original.exists()
        assert (original / "whisper-server.exe").read_text(encoding="utf-8") == "OLD"
        assert not backup.exists()
    finally:
        _sh.rmtree(base, ignore_errors=True)


# --- existence probes + backend marker -------------------------------------

def test_backend_marker_roundtrip():
    import tempfile, shutil as _sh
    base = Path(tempfile.mkdtemp())
    try:
        assert pw.read_backend_marker(base) is None
        pw.write_backend_marker(base, "vulkan")
        assert pw.read_backend_marker(base) == "vulkan"
    finally:
        _sh.rmtree(base, ignore_errors=True)


def test_whisper_present_requires_matching_backend():
    import tempfile, shutil as _sh
    base = Path(tempfile.mkdtemp())
    try:
        rel = base / "tools" / "whisper.cpp" / "bin" / "Release"
        rel.mkdir(parents=True)
        (rel / "whisper-server.exe").write_text("x", encoding="utf-8")
        pw.write_backend_marker(base, "cpu")
        assert pw.whisper_present(base, "cpu") is True
        assert pw.whisper_present(base, "vulkan") is False  # different backend
    finally:
        _sh.rmtree(base, ignore_errors=True)


def test_model_and_espeak_present():
    import tempfile, shutil as _sh
    base = Path(tempfile.mkdtemp())
    try:
        assert pw.model_present(base, "ggml-medium-q5_0") is False
        m = base / "tools" / "whisper.cpp" / "models"
        m.mkdir(parents=True)
        (m / "ggml-medium-q5_0.bin").write_text("data", encoding="utf-8")
        assert pw.model_present(base, "ggml-medium-q5_0") is True
        assert pw.espeak_present(base) is False
        e = base / "tools" / "espeak-ng"
        e.mkdir(parents=True)
        (e / "espeak-ng.exe").write_text("x", encoding="utf-8")
        assert pw.espeak_present(base) is True
    finally:
        _sh.rmtree(base, ignore_errors=True)


def test_vulkan_sdk_present_via_env_and_dir():
    import tempfile, os as _os, shutil as _sh
    base = Path(tempfile.mkdtemp())
    try:
        saved = _os.environ.pop("VULKAN_SDK", None)
        _os.environ["VULKAN_SDK"] = str(base)
        assert pw.vulkan_sdk_present(base=Path("C:/nonexistent-xyz")) is True
        del _os.environ["VULKAN_SDK"]
        empty = base / "empty"
        empty.mkdir()
        assert pw.vulkan_sdk_present(base=empty) is False
        (empty / "1.3.0").mkdir()
        assert pw.vulkan_sdk_present(base=empty) is True
    finally:
        if saved is not None:
            _os.environ["VULKAN_SDK"] = saved
        else:
            _os.environ.pop("VULKAN_SDK", None)
        _sh.rmtree(base, ignore_errors=True)


# --- main --detect-only (no network; detect_gpu monkeypatched) -------------

def test_main_detect_only_prints_plan():
    import tempfile, shutil as _sh, io, contextlib
    base = Path(tempfile.mkdtemp())
    saved = pw.detect_gpu
    try:
        pw.detect_gpu = lambda: pw.GpuInfo(vendor="none", name="Basic Display")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pw.main(["--project-dir", str(base), "--gpu", "auto", "--detect-only"])
        out = buf.getvalue()
        assert rc == 0
        assert "cpu" in out.lower()
        assert "Download whisper.cpp CPU/BLAS binary" in out
    finally:
        pw.detect_gpu = saved
        _sh.rmtree(base, ignore_errors=True)


# --- pick_adapter_name --------------------------------------------------------

def test_pick_adapter_name_nvidia_on_dual_gpu():
    names = ["Intel(R) UHD Graphics", "NVIDIA GeForce RTX 3050"]
    assert pw.pick_adapter_name(names, "nvidia") == "NVIDIA GeForce RTX 3050"


def test_pick_adapter_name_amd_radeon():
    names = ["AMD Radeon RX 6800"]
    assert pw.pick_adapter_name(names, "amd") == "AMD Radeon RX 6800"


def test_pick_adapter_name_intel():
    names = ["Intel(R) UHD Graphics 770"]
    assert pw.pick_adapter_name(names, "intel") == "Intel(R) UHD Graphics 770"


def test_pick_adapter_name_fallback_when_no_match():
    # vendor classified as "none" (or any unrecognised) -> first name
    names = ["Microsoft Basic Display Adapter"]
    assert pw.pick_adapter_name(names, "none") == "Microsoft Basic Display Adapter"


def test_pick_adapter_name_empty_returns_empty():
    assert pw.pick_adapter_name([], "nvidia") == ""


def _run_all() -> int:
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in funcs:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
