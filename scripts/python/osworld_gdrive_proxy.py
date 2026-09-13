#!/usr/bin/env python3
"""Run a private Clash instance for OSWorld Google Drive recovery tasks.

The runtime copy of the Clash configuration is mode 0600, binds only to the
Docker bridge, and is removed on shutdown. Proxy node names, addresses, and
credentials are deliberately never logged.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
import yaml


GOOGLE_HEALTH_URL = "https://www.gstatic.com/generate_204"
GOOGLE_REACHABILITY_URLS = {
    "https://oauth2.googleapis.com/": {404},
    "https://www.googleapis.com/drive/v2/files": {401, 403},
    "https://accounts.google.com/": set(range(200, 400)),
    "https://drive.google.com/": set(range(200, 400)),
}


class ProxyStartupError(RuntimeError):
    pass


def validate_proxy_url(proxy_url: str) -> str:
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("Proxy URL must use http, https, socks5, or socks5h")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("Proxy URL must include a host and port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Authenticated proxy URLs are not accepted on the command line")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Proxy URL must not contain a path, query, or fragment")
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def docker_bridge_gateway(network_name: str = "bridge") -> str:
    try:
        import docker

        client = docker.from_env()
        try:
            config = client.networks.get(network_name).attrs["IPAM"]["Config"]
        finally:
            client.close()
        gateway = next(item.get("Gateway") for item in config if item.get("Gateway"))
    except Exception as exc:
        raise ProxyStartupError(
            "Could not determine the Docker bridge gateway; pass --bind-host explicitly"
        ) from exc

    address = ipaddress.ip_address(gateway)
    if not address.is_private:
        raise ProxyStartupError("Docker bridge gateway is not a private address")
    return str(address)


def _assert_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, port))


class ManagedClashProxy:
    """Own one isolated Clash process and keep a healthy node selected."""

    def __init__(
        self,
        *,
        binary: Path,
        config: Path,
        bind_host: str | None = None,
        proxy_port: int = 27891,
        controller_port: int = 29091,
        health_timeout_ms: int = 6000,
        monitor_interval: float = 60.0,
    ) -> None:
        self.binary = Path(binary).expanduser().resolve()
        self.config = Path(config).expanduser().resolve()
        self.bind_host = bind_host or docker_bridge_gateway()
        self.proxy_port = proxy_port
        self.controller_port = controller_port
        self.health_timeout_ms = health_timeout_ms
        self.monitor_interval = monitor_interval

        self.runtime_dir: Path | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._selection_lock = threading.Lock()
        self._selected_name: str | None = None
        self._healthy_count = 0
        self._selected_latency_ms: int | None = None

    @property
    def host_proxy_url(self) -> str:
        return validate_proxy_url(f"http://{self.bind_host}:{self.proxy_port}")

    @property
    def vm_proxy_url(self) -> str:
        return self.host_proxy_url

    @property
    def controller_url(self) -> str:
        return f"http://127.0.0.1:{self.controller_port}"

    @property
    def healthy_count(self) -> int:
        return self._healthy_count

    @property
    def selected_latency_ms(self) -> int | None:
        return self._selected_latency_ms

    def __enter__(self) -> "ManagedClashProxy":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _load_source_config(self) -> dict[str, Any]:
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise ProxyStartupError("Clash binary is missing or not executable")
        if not self.config.is_file():
            raise ProxyStartupError("Clash configuration file is missing")
        try:
            with self.config.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except Exception as exc:
            raise ProxyStartupError("Clash configuration could not be parsed") from exc
        if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
            raise ProxyStartupError("Clash configuration has no proxy list")
        return data

    def _write_runtime_config(self, source: dict[str, Any]) -> Path:
        runtime_dir = Path(tempfile.mkdtemp(prefix="osworld-gdrive-clash-"))
        runtime_dir.chmod(0o700)
        self.runtime_dir = runtime_dir
        try:
            runtime_config = dict(source)
            runtime_config.update(
                {
                    "mixed-port": self.proxy_port,
                    "allow-lan": True,
                    "bind-address": self.bind_host,
                    "external-controller": f"127.0.0.1:{self.controller_port}",
                    "log-level": "warning",
                }
            )
            config_path = runtime_dir / "config.yaml"
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    runtime_config, handle, allow_unicode=True, sort_keys=False
                )
            config_path.chmod(0o600)

            country_db = self.config.parent / "Country.mmdb"
            if country_db.is_file():
                target = runtime_dir / "Country.mmdb"
                shutil.copyfile(country_db, target)
                target.chmod(0o600)
            return config_path
        except Exception as exc:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            self.runtime_dir = None
            raise ProxyStartupError("Private Clash runtime could not be created") from exc

    def _api(self, method: str, path: str, **kwargs) -> requests.Response:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.request(
                method,
                self.controller_url + path,
                timeout=kwargs.pop("timeout", 5),
                **kwargs,
            )
            response.raise_for_status()
            return response
        finally:
            session.close()

    def _wait_for_controller(self) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise ProxyStartupError("Clash exited during startup")
            try:
                self._api("GET", "/version", timeout=1)
                return
            except Exception:
                time.sleep(0.25)
        raise ProxyStartupError("Clash controller did not become ready")

    def _delay_for(self, name: str) -> int | None:
        try:
            response = self._api(
                "GET",
                f"/proxies/{quote(name, safe='')}/delay",
                params={
                    "url": GOOGLE_HEALTH_URL,
                    "timeout": self.health_timeout_ms,
                },
                timeout=(self.health_timeout_ms / 1000) + 3,
            )
            delay = int(response.json().get("delay", 0))
            return delay if delay > 0 else None
        except Exception:
            return None

    def _select_healthy_node(self, source: dict[str, Any]) -> None:
        with self._selection_lock:
            names = [
                item.get("name")
                for item in source.get("proxies", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            if not names:
                raise ProxyStartupError("Clash configuration has no named proxy nodes")

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(16, len(names))
            ) as executor:
                delays = list(executor.map(self._delay_for, names))
            healthy = sorted(
                (delay, name)
                for name, delay in zip(names, delays)
                if delay is not None
            )
            if not healthy:
                raise ProxyStartupError("No healthy Clash nodes passed the Google check")

            latency, selected = healthy[0]
            proxy_objects = self._api("GET", "/proxies").json().get("proxies", {})
            changed = 0
            for group_name, group in proxy_objects.items():
                if (
                    isinstance(group, dict)
                    and group.get("type") == "Selector"
                    and selected in group.get("all", [])
                ):
                    self._api(
                        "PUT",
                        f"/proxies/{quote(group_name, safe='')}",
                        json={"name": selected},
                    )
                    changed += 1
            if changed == 0:
                raise ProxyStartupError("No selector group accepted a healthy Clash node")

            self._selected_name = selected
            self._healthy_count = len(healthy)
            self._selected_latency_ms = latency

    def _verify_google_reachability(self) -> None:
        session = requests.Session()
        session.trust_env = False
        proxies = {
            "http": self.host_proxy_url,
            "https": self.host_proxy_url,
        }
        try:
            for url, accepted_statuses in GOOGLE_REACHABILITY_URLS.items():
                response = session.get(
                    url,
                    proxies=proxies,
                    allow_redirects=True,
                    timeout=(8, 25),
                )
                if response.status_code not in accepted_statuses:
                    raise ProxyStartupError("Google reachability check returned an error")
        except ProxyStartupError:
            raise
        except Exception as exc:
            raise ProxyStartupError("Google is not reachable through Clash") from exc
        finally:
            session.close()

    def _monitor(self, source: dict[str, Any]) -> None:
        while not self._stop_event.wait(self.monitor_interval):
            selected = self._selected_name
            if self.process is None or self.process.poll() is not None:
                return
            if selected is not None and self._delay_for(selected) is not None:
                continue
            try:
                self._select_healthy_node(source)
                self._verify_google_reachability()
            except Exception:
                # A later monitor pass retries all nodes. Details could contain
                # node metadata, so they are intentionally not emitted.
                continue

    def start(self) -> "ManagedClashProxy":
        if self.process is not None:
            raise ProxyStartupError("Clash proxy is already started")

        address = ipaddress.ip_address(self.bind_host)
        if not (address.is_private or address.is_loopback):
            raise ProxyStartupError("Clash must bind to a private or loopback address")
        try:
            _assert_port_available(self.bind_host, self.proxy_port)
            _assert_port_available("127.0.0.1", self.controller_port)
        except OSError as exc:
            raise ProxyStartupError("Requested Clash port is unavailable") from exc

        self._stop_event.clear()

        source = self._load_source_config()
        runtime_config = self._write_runtime_config(source)
        assert self.runtime_dir is not None
        log_path = self.runtime_dir / "clash.log"
        self._log_handle = log_path.open("wb")
        log_path.chmod(0o600)

        try:
            self.process = subprocess.Popen(
                [
                    str(self.binary),
                    "-d",
                    str(self.runtime_dir),
                    "-f",
                    str(runtime_config),
                ],
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._wait_for_controller()
            self._select_healthy_node(source)
            self._verify_google_reachability()
            self._monitor_thread = threading.Thread(
                target=self._monitor,
                args=(source,),
                name="osworld-gdrive-proxy-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
            self._monitor_thread = None

        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None

        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

        if self.runtime_dir is not None:
            runtime_dir = self.runtime_dir.resolve()
            if runtime_dir.parent == Path(tempfile.gettempdir()).resolve() and runtime_dir.name.startswith(
                "osworld-gdrive-clash-"
            ):
                shutil.rmtree(runtime_dir, ignore_errors=True)
            self.runtime_dir = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bind-host")
    parser.add_argument("--proxy-port", type=int, default=27891)
    parser.add_argument("--controller-port", type=int, default=29091)
    parser.add_argument("--monitor-interval", type=float, default=60.0)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check the proxy and exit instead of keeping it running",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    stop_event = threading.Event()

    def request_stop(signum, frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        with ManagedClashProxy(
            binary=args.binary,
            config=args.config,
            bind_host=args.bind_host,
            proxy_port=args.proxy_port,
            controller_port=args.controller_port,
            monitor_interval=args.monitor_interval,
        ) as proxy:
            print(
                "READY: Google reachable; "
                f"healthy_nodes={proxy.healthy_count}; "
                f"selected_latency_ms={proxy.selected_latency_ms}; "
                f"proxy={proxy.host_proxy_url}",
                flush=True,
            )
            if not args.once:
                while not stop_event.wait(1):
                    if proxy.process is None or proxy.process.poll() is not None:
                        raise ProxyStartupError("Clash stopped unexpectedly")
        return 0
    except (ProxyStartupError, ValueError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1
    except Exception:
        print("ERROR: unexpected private proxy failure", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
