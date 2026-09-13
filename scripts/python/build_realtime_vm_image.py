"""Bake the realtime server into a new Docker OSWorld qcow2 image.

The Docker provider mounts the source image read-only and gives QEMU a
temporary overlay. This helper preserves the source, installs the realtime
extension into that overlay, then flattens it into a new qcow2 file.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import requests

# Allow both `python scripts/python/...` and `python -m ...` entry points.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.python.install_realtime_server import install


DOCKER_IMAGE = "happysixd/osworld-docker"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, timeout_s: float = 300, container_name: str | None = None) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if requests.get(url + "/screenshot", timeout=3).ok:
                return
        except requests.RequestException:
            if container_name:
                state = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                    capture_output=True, text=True, check=False,
                )
                if state.returncode == 0 and state.stdout.strip() == "false":
                    raise RuntimeError(
                        "The temporary VM exited before its service became ready; "
                        "inspect it with docker logs " + container_name
                    )
        time.sleep(1)
    raise TimeoutError("The temporary VM did not expose its OSWorld service")


def graceful_shutdown(container_name: str, timeout_s: float = 90) -> None:
    """Ask QEMU/Ubuntu to flush the guest before extracting its overlay."""
    command = (
        "import socket; s=socket.create_connection(('127.0.0.1',7100),5); "
        "s.recv(4096); s.sendall(b'system_powerdown\\n'); s.close()"
    )
    subprocess.run(
        ["docker", "exec", container_name, "python3", "-c", command],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, check=False,
        )
        if state.returncode != 0 or state.stdout.strip() == "false":
            return
        time.sleep(1)
    subprocess.run(["docker", "stop", "-t", "30", container_name], check=True)


def verify_image(image: Path, timeout_s: float = 300) -> None:
    """Boot the baked image and require its realtime endpoint before returning."""
    port = free_port()
    name = f"osworld-realtime-image-verify-{os.getpid()}"
    try:
        subprocess.check_call([
            "docker", "run", "-d", "--name", name, "--cap-add", "NET_ADMIN",
            "--device", "/dev/kvm", "-e", "DISK_SIZE=32G", "-e", "RAM_SIZE=4G",
            "-e", "CPU_CORES=4", "-p", f"127.0.0.1:{port}:5000",
            "-v", f"{image}:/System.qcow2:ro", DOCKER_IMAGE,
        ], stdout=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        wait_for_server(base, timeout_s=timeout_s, container_name=name)
        response = requests.get(base + "/realtime/capabilities", timeout=5)
        response.raise_for_status()
        capabilities = response.json()
        if capabilities.get("fmp4") is not True or capabilities.get("sequence") is not True:
            raise RuntimeError(f"Baked image lacks realtime capabilities: {capabilities}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)


def build(source: Path, output: Path, password: str = "", *, force=False, verify_timeout_s=300) -> Path:
    source = source.resolve()
    output = output.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source == output:
        raise ValueError("Output must be a new image; the source is never modified")
    if output.exists() and not force:
        raise FileExistsError(f"Refusing to replace {output}; use --force to replace it")
    if shutil.which("docker") is None:
        raise RuntimeError("docker is required")

    port = free_port()
    name = f"osworld-realtime-image-build-{os.getpid()}"
    container = None
    temporary_dir = Path(tempfile.mkdtemp(prefix="osworld-realtime-image-"))
    overlay = temporary_dir / "boot.qcow2"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        docker_args = [
            "docker", "run", "-d", "--name", name,
            "--cap-add", "NET_ADMIN",
            "-e", "DISK_SIZE=32G", "-e", "RAM_SIZE=4G", "-e", "CPU_CORES=4",
            "-p", f"127.0.0.1:{port}:5000",
            "-v", f"{source}:/System.qcow2:ro", DOCKER_IMAGE,
        ]
        if Path("/dev/kvm").exists():
            docker_args.insert(docker_args.index(DOCKER_IMAGE), "--device")
            docker_args.insert(docker_args.index(DOCKER_IMAGE), "/dev/kvm")
        else:
            docker_args.insert(docker_args.index(DOCKER_IMAGE), "KVM=N")
            docker_args.insert(docker_args.index(DOCKER_IMAGE), "-e")
        container = subprocess.check_output(docker_args, text=True).strip()
        wait_for_server(f"http://127.0.0.1:{port}", container_name=name)
        capabilities = install(f"http://127.0.0.1:{port}", password=password)
        if capabilities.get("fmp4") is not True or capabilities.get("sequence") is not True:
            raise RuntimeError(f"Realtime extension did not report all capabilities: {capabilities}")
        # Stop first so QEMU flushes its overlay, but copy before removing the
        # container (the overlay lives in its writable container layer).
        graceful_shutdown(name)
        subprocess.run(["docker", "cp", f"{name}:/boot.qcow2", str(overlay)], check=True)
        if not overlay.is_file() or overlay.stat().st_size == 0:
            raise RuntimeError("The temporary VM did not produce /boot.qcow2")
        # Keep the original qcow2 layout and commit the overlay into a copy.
        # Converting the overlay into a fresh qcow2 can change boot metadata on
        # some UEFI images even though qemu-img reports the file as valid.
        if output.exists():
            output.unlink()
        subprocess.run(["cp", "--reflink=auto", str(source), str(output)], check=True)
        utility = [
            "docker", "run", "--rm", "--entrypoint", "qemu-img",
            "-v", f"{overlay}:/overlay.qcow2:rw",
            "-v", f"{output.parent}:/out", DOCKER_IMAGE,
        ]
        target_inside = f"/out/{output.name}"
        subprocess.run(
            utility + ["rebase", "-u", "-b", target_inside, "-F", "qcow2", "/overlay.qcow2"],
            check=True,
        )
        subprocess.run(utility + ["commit", "/overlay.qcow2"], check=True)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("qemu-img did not create the output image")
        try:
            verify_image(output, timeout_s=verify_timeout_s)
        except Exception:
            # Never leave an image that was not boot-tested available for use.
            output.unlink(missing_ok=True)
            raise
        return output
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)
        shutil.rmtree(temporary_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("docker_vm_data/Ubuntu-realtime-gui.qcow2"))
    parser.add_argument("--output", type=Path, default=Path("docker_vm_data/Ubuntu-realtime-gui-fmp4.qcow2"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verify-timeout-s", type=float, default=300)
    args = parser.parse_args()
    print(build(args.source, args.output, os.getenv("OSWORLD_VM_PASSWORD", ""),
                force=args.force, verify_timeout_s=args.verify_timeout_s))


if __name__ == "__main__":
    main()
