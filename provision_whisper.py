"""claude-speech whisper.cpp binary provisioner.

Detects the GPU and provisions the right whisper.cpp backend into
<project-dir>/tools/, plus the ggml model and espeak-ng:
  - NVIDIA  -> download a prebuilt CUDA zip (no compile)
  - AMD/Intel -> compile the Vulkan backend from source
  - no GPU  -> download the prebuilt CPU/BLAS zip

Interactive consent is handled by the caller (the skill / install.py); this
script prints a plan with --detect-only and does the work with --gpu. On any
failure it rolls back the artifacts it created THIS run (never system SDKs)
and exits non-zero. Windows-only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("provision_whisper")


def parse_video_controllers(text: str) -> list[str]:
    """Names from `Get-CimInstance Win32_VideoController -ExpandProperty Name`."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def classify_vendor(names: list[str]) -> str:
    """Return nvidia | amd | intel | none. A discrete card (NVIDIA/AMD) wins
    over an integrated Intel adapter, so priority is nvidia > amd > intel."""
    joined = " ".join(names).lower()
    if "nvidia" in joined:
        return "nvidia"
    if "amd" in joined or "radeon" in joined:
        return "amd"
    if "intel" in joined:
        return "intel"
    return "none"


def pick_adapter_name(names: list[str], vendor: str) -> str:
    """Name of the adapter matching the classified vendor (so a dual-GPU
    laptop shows the winning dGPU, not the first-listed iGPU). Falls back
    to the first name."""
    for name in names:
        low = name.lower()
        if vendor == "nvidia" and "nvidia" in low:
            return name
        if vendor == "amd" and ("amd" in low or "radeon" in low):
            return name
        if vendor == "intel" and "intel" in low:
            return name
    return names[0] if names else ""


WHISPER_TAG = "v1.8.4"
_REL = f"https://github.com/ggerganov/whisper.cpp/releases/download/{WHISPER_TAG}"
CPU_ZIP = f"{_REL}/whisper-blas-bin-x64.zip"
CUDA_124_ZIP = f"{_REL}/whisper-cublas-12.4.0-bin-x64.zip"
CUDA_118_ZIP = f"{_REL}/whisper-cublas-11.8.0-bin-x64.zip"
MODEL_URL_TMPL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{name}.bin"
ESPEAK_MSI = "https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi"
WHISPER_SRC = "https://github.com/ggerganov/whisper.cpp"

_CUDA_RE = re.compile(r"CUDA Version:\s*([0-9]+\.[0-9]+)")


def parse_cuda_version(text: str) -> str | None:
    match = _CUDA_RE.search(text or "")
    return match.group(1) if match else None


def cuda_zip_url(cuda_version: str | None) -> str:
    """Pick the CUDA 11.8 build only for an 11.x driver; otherwise the 12.4 build."""
    # CUDA 10.x and 13+ are intentionally bucketed into the 12.4 build.
    if cuda_version and cuda_version.split(".")[0] == "11":
        return CUDA_118_ZIP
    return CUDA_124_ZIP


def choose_backend(vendor: str, requested: str) -> str:
    """requested in {auto, cpu, cuda, vulkan}. auto maps vendor -> backend."""
    if requested != "auto":
        return requested
    return {"nvidia": "cuda", "amd": "vulkan", "intel": "vulkan"}.get(vendor, "cpu")


@dataclass
class GpuInfo:
    vendor: str
    name: str = ""
    cuda_version: str | None = None


@dataclass
class Step:
    key: str
    description: str
    size_mb: int
    est_seconds: int
    skip: bool = False
    reason: str = ""


def _step(key: str, desc: str, size_mb: int, est_s: int, present: bool) -> Step:
    return Step(key, desc, size_mb, est_s, skip=present,
                reason="already installed" if present else "")


def build_plan(backend: str, gpu: GpuInfo, probes: dict) -> list[Step]:
    steps: list[Step] = []
    whisper_present = probes.get("whisper", False)
    if backend == "cpu":
        steps.append(_step("whisper-cpu", "Download whisper.cpp CPU/BLAS binary",
                           16, 20, whisper_present))
    elif backend == "cuda":
        zip_name = cuda_zip_url(gpu.cuda_version).rsplit("/", 1)[-1]
        steps.append(_step("whisper-cuda", f"Download whisper.cpp CUDA binary ({zip_name})",
                           460, 60, whisper_present))
    elif backend == "vulkan":
        steps.append(_step("vs-build-tools",
                           "winget install VS 2022 Build Tools (VCTools + CMake + Win11 SDK)",
                           6000, 600, probes.get("vsbuildtools", False)))
        steps.append(_step("vulkan-sdk", "winget install Vulkan SDK",
                           400, 120, probes.get("vulkan_sdk", False)))
        steps.append(_step("whisper-vulkan",
                           "git clone whisper.cpp@v1.8.4 + cmake build (Vulkan)",
                           200, 480, whisper_present))
    else:
        raise ValueError(f"unknown backend {backend!r}")
    steps.append(_step("model", "Download ggml model", 540, 120, probes.get("model", False)))
    steps.append(_step("espeak", "Download + extract espeak-ng", 80, 30, probes.get("espeak", False)))
    return steps


def plan_to_text(steps: list[Step]) -> str:
    lines = []
    for s in steps:
        tag = "SKIP" if s.skip else "DO  "
        extra = f" ({s.reason})" if s.reason else ""
        lines.append(f"  [{tag}] {s.description} — ~{s.size_mb} MB, ~{s.est_seconds}s{extra}")
    total_mb = sum(s.size_mb for s in steps if not s.skip)
    total_s = sum(s.est_seconds for s in steps if not s.skip)
    lines.append(f"  Total to do: ~{total_mb} MB, ~{total_s}s")
    return "\n".join(lines)


def plan_to_json(steps: list[Step]) -> str:
    return json.dumps([asdict(s) for s in steps], ensure_ascii=False, indent=2)


class Rollbacker:
    """Records paths created this run and backups taken this run, so a failure
    can undo exactly those — never pre-existing content or system SDKs."""

    def __init__(self) -> None:
        self._paths: list[Path] = []
        self._backups: list[tuple[Path, Path]] = []  # (original, backup)

    def track(self, path) -> None:
        self._paths.append(Path(path))

    def track_backup(self, original, backup) -> None:
        self._backups.append((Path(original), Path(backup)))

    def rollback(self) -> None:
        # Remove freshly-created paths first (reverse order).
        for path in reversed(self._paths):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        # Then restore any backed-up originals.
        for original, backup in reversed(self._backups):
            if backup.exists():
                if original.exists():
                    if original.is_dir():
                        shutil.rmtree(original, ignore_errors=True)
                    else:
                        original.unlink()
                backup.rename(original)
        self._paths.clear()
        self._backups.clear()


def _release_dir(project_dir) -> Path:
    return Path(project_dir) / "tools" / "whisper.cpp" / "bin" / "Release"


def _marker_path(project_dir) -> Path:
    return _release_dir(project_dir) / ".backend"


def read_backend_marker(project_dir) -> str | None:
    marker = _marker_path(project_dir)
    return marker.read_text(encoding="utf-8").strip() if marker.exists() else None


def write_backend_marker(project_dir, backend: str) -> None:
    marker = _marker_path(project_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(backend, encoding="utf-8")


def whisper_present(project_dir, backend: str) -> bool:
    exe = _release_dir(project_dir) / "whisper-cli.exe"
    return exe.exists() and read_backend_marker(project_dir) == backend


def model_present(project_dir, model_name: str) -> bool:
    path = Path(project_dir) / "tools" / "whisper.cpp" / "models" / (model_name + ".bin")
    return path.exists() and path.stat().st_size > 0


def espeak_present(project_dir) -> bool:
    return (Path(project_dir) / "tools" / "espeak-ng" / "espeak-ng.exe").exists()


def vs_build_tools_present() -> bool:
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if not vswhere.exists():
        return False
    try:
        out = subprocess.run(
            [str(vswhere), "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=30,
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def vulkan_sdk_present(base: Path | None = None) -> bool:
    if os.environ.get("VULKAN_SDK"):
        return True
    resolved = base if base is not None else Path(r"C:\VulkanSDK")
    return resolved.exists() and any(resolved.iterdir())


def _run_ps(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True, text=True, timeout=60,
    )


def detect_gpu() -> GpuInfo:
    try:
        out = _run_ps("Get-CimInstance Win32_VideoController | "
                      "Select-Object -ExpandProperty Name")
        names = parse_video_controllers(out.stdout)
    except (OSError, subprocess.SubprocessError):
        names = []
    vendor = classify_vendor(names)
    cuda = None
    if vendor == "nvidia":
        try:
            smi = subprocess.run(["nvidia-smi"], capture_output=True,
                                 text=True, timeout=30)
            cuda = parse_cuda_version(smi.stdout)
        except (OSError, subprocess.SubprocessError):
            cuda = None
    return GpuInfo(vendor=vendor, name=pick_adapter_name(names, vendor), cuda_version=cuda)


def download_file(url: str, dest: Path, rb: Rollbacker, min_bytes: int = 1024) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rb.track(dest)
    logger.info("downloading %s -> %s", url, dest)
    urllib.request.urlretrieve(url, dest)
    size = dest.stat().st_size
    if size < min_bytes:
        raise RuntimeError(f"download too small ({size} bytes) from {url}")
    return dest


def extract_zip(zip_path: Path, dest_dir: Path, rb: Rollbacker) -> None:
    dest_dir = Path(dest_dir)
    if not dest_dir.exists():
        rb.track(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)


def _backup_existing_release(project_dir, rb: Rollbacker) -> None:
    """If a whisper binary exists, move its Release dir aside so a failed
    backend switch can be rolled back to the working one."""
    release = _release_dir(project_dir)
    if release.exists():
        backup = release.with_name("Release.bak")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        release.rename(backup)
        rb.track_backup(release, backup)


def fetch_whisper_prebuilt(project_dir, url: str, rb: Rollbacker) -> None:
    """CPU or CUDA path: download a release zip and extract to tools/whisper.cpp/bin."""
    _backup_existing_release(project_dir, rb)
    bin_dir = Path(project_dir) / "tools" / "whisper.cpp" / "bin"
    zip_path = Path(project_dir) / "tools" / "whisper.cpp" / "_whisper.zip"
    download_file(url, zip_path, rb, min_bytes=1_000_000)
    extract_zip(zip_path, bin_dir, rb)
    zip_path.unlink(missing_ok=True)


def fetch_model(project_dir, model_name: str, rb: Rollbacker) -> None:
    dest = Path(project_dir) / "tools" / "whisper.cpp" / "models" / (model_name + ".bin")
    download_file(MODEL_URL_TMPL.format(name=model_name), dest, rb, min_bytes=50_000_000)


def fetch_espeak(project_dir, rb: Rollbacker) -> None:
    tools = Path(project_dir) / "tools"
    msi = tools / "espeak-ng.msi"
    extract = tools / "espeak-extract"
    download_file(ESPEAK_MSI, msi, rb, min_bytes=1_000_000)
    rb.track(extract)
    proc = subprocess.run(
        ["msiexec.exe", "/a", str(msi), "/qn", f"TARGETDIR={extract}"],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"msiexec admin-extract failed: {proc.stdout}\n{proc.stderr}")
    nested = extract / "eSpeak NG"
    target = tools / "espeak-ng"
    rb.track(target)
    shutil.move(str(nested), str(target))
    shutil.rmtree(extract, ignore_errors=True)
    msi.unlink(missing_ok=True)


def _winget_install(package_id: str, override: str | None = None) -> None:
    cmd = ["winget", "install", "--id", package_id, "--silent",
           "--accept-source-agreements", "--accept-package-agreements"]
    if override:
        cmd += ["--override", override]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"winget install {package_id} failed: {proc.stdout}\n{proc.stderr}")


def ensure_vs_build_tools() -> None:
    if vs_build_tools_present():
        logger.info("VS Build Tools already present; skipping")
        return
    _winget_install(
        "Microsoft.VisualStudio.2022.BuildTools",
        override=("--quiet --wait --norestart "
                  "--add Microsoft.VisualStudio.Workload.VCTools "
                  "--add Microsoft.VisualStudio.Component.Windows11SDK.22621 "
                  "--add Microsoft.VisualStudio.Component.VC.CMake.Project "
                  "--includeRecommended"),
    )


def ensure_vulkan_sdk() -> None:
    if vulkan_sdk_present():
        logger.info("Vulkan SDK already present; skipping")
        return
    _winget_install("KhronosGroup.VulkanSDK")


def _find_cmake() -> str:
    candidate = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
                     r"\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("cmake")
    if found:
        return found
    raise RuntimeError("cmake.exe not found (VS Build Tools CMake component missing)")


def build_vulkan(project_dir, rb: Rollbacker) -> None:
    tools = Path(project_dir) / "tools"
    src = tools / "whisper.cpp-src"
    rb.track(src)
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", WHISPER_TAG, WHISPER_SRC, str(src)],
        capture_output=True, text=True, timeout=600,
    )
    if clone.returncode != 0:
        raise RuntimeError(f"git clone failed: {clone.stdout}\n{clone.stderr}")
    cmake = _find_cmake()
    env = dict(os.environ)
    vk_root = Path(r"C:\VulkanSDK")
    if not env.get("VULKAN_SDK") and vk_root.exists():
        latest = sorted(p for p in vk_root.iterdir() if p.is_dir())[-1]
        env["VULKAN_SDK"] = str(latest)
    cfg = subprocess.run([cmake, "-B", "build", "-DGGML_VULKAN=ON"],
                         cwd=src, env=env, capture_output=True, text=True, timeout=600)
    if cfg.returncode != 0:
        raise RuntimeError(f"cmake configure failed: {cfg.stdout}\n{cfg.stderr}")
    build = subprocess.run([cmake, "--build", "build", "--config", "Release", "-j"],
                           cwd=src, env=env, capture_output=True, text=True, timeout=1800)
    if build.returncode != 0:
        raise RuntimeError(f"cmake build failed: {build.stdout}\n{build.stderr}")
    _backup_existing_release(project_dir, rb)
    release = _release_dir(project_dir)
    rb.track(release)
    release.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "build" / "bin" / "Release", release)


DEFAULT_MODEL = "ggml-medium-q5_0"


def gather_probes(project_dir, backend: str, model_name: str) -> dict:
    return {
        "whisper": whisper_present(project_dir, backend),
        "model": model_present(project_dir, model_name),
        "espeak": espeak_present(project_dir),
        "vsbuildtools": vs_build_tools_present(),
        "vulkan_sdk": vulkan_sdk_present(),
    }


def _setup_logging(project_dir) -> None:
    log_path = Path(project_dir) / "logs" / "provision_whisper.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")


def provision(project_dir, backend: str, gpu: GpuInfo, model_name: str) -> None:
    """Execute the plan. Raises on any failure (caller rolls back)."""
    rb = Rollbacker()
    try:
        if backend == "vulkan":
            ensure_vs_build_tools()
            ensure_vulkan_sdk()
            if not whisper_present(project_dir, backend):
                build_vulkan(project_dir, rb)
        elif backend == "cuda":
            if gpu.cuda_version and gpu.cuda_version.split(".")[0] == "11":
                msg = ("WARNING: CUDA 11.x detected. The 11.8 whisper build does not bundle "
                       "cuDNN; install cuDNN 8.x manually (see README 'Optional: CUDA build') "
                       "or whisper-cli.exe will fail to start.")
                logger.warning(msg)
                print(msg, file=sys.stderr)
            if not whisper_present(project_dir, backend):
                fetch_whisper_prebuilt(project_dir, cuda_zip_url(gpu.cuda_version), rb)
        else:  # cpu
            if not whisper_present(project_dir, backend):
                fetch_whisper_prebuilt(project_dir, CPU_ZIP, rb)
        write_backend_marker(project_dir, backend)
        if not model_present(project_dir, model_name):
            fetch_model(project_dir, model_name, rb)
        if not espeak_present(project_dir):
            fetch_espeak(project_dir, rb)
    except Exception:
        logger.exception("provisioning failed; rolling back in-project artifacts")
        rb.rollback()
        raise


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="claude-speech whisper.cpp provisioner")
    parser.add_argument("--project-dir", required=True, help="project root (tools/ lives here)")
    parser.add_argument("--gpu", choices=["auto", "cpu", "cuda", "vulkan"], default="auto")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="ggml model name (no .bin)")
    parser.add_argument("--detect-only", action="store_true",
                        help="print the detected GPU + plan (text and JSON) and exit")
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    gpu = detect_gpu()
    backend = choose_backend(gpu.vendor, args.gpu)
    probes = gather_probes(project_dir, backend, args.model)
    plan = build_plan(backend, gpu, probes)

    print(f"Detected GPU: {gpu.vendor} ({gpu.name or 'unknown'})"
          + (f", CUDA {gpu.cuda_version}" if gpu.cuda_version else ""))
    print(f"Backend: {backend}\nPlan:")
    print(plan_to_text(plan))

    if args.detect_only:
        print("\nPLAN_JSON:")
        print(plan_to_json(plan))
        return 0

    _setup_logging(project_dir)
    try:
        provision(project_dir, backend, gpu, args.model)
    except Exception as exc:  # noqa: BLE001
        log_path = Path(project_dir) / "logs" / "provision_whisper.log"
        print(f"\nERROR: provisioning failed: {exc}", file=sys.stderr)
        print(f"In-project artifacts were rolled back. System SDKs were left installed.\n"
              f"See {log_path}. You can re-run choosing CPU: "
              f"py provision_whisper.py --project-dir \"{project_dir}\" --gpu cpu",
              file=sys.stderr)
        return 1
    print(f"\nDone. whisper.cpp ({backend}) + model + espeak-ng are in {project_dir}\\tools.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
