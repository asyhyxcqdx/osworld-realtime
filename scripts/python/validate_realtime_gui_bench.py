#!/usr/bin/env python3
"""Validate RealtimeGUI-Bench initial states and capture review screenshots."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBSITE_ROOT = (
    PROJECT_ROOT / "evaluation_examples/websites/realtime_gui_bench/games"
)
DEFAULT_BROWSER = Path(
    "/mnt/zhaorunsong/.cache/ms-playwright/chromium_headless_shell-1208/"
    "chrome-headless-shell-linux64/chrome-headless-shell"
)
ENTRY_LABELS = {
    "begin",
    "drop",
    "play",
    "reveal",
    "shuffle",
    "spin",
    "start",
}


def benchmark_ids() -> list[str]:
    return sorted(
        (path.parent.name for path in WEBSITE_ROOT.glob("*/index.html")),
        key=lambda item: (item[0], int(item[1:])),
    )


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def page_state(page) -> dict[str, Any]:
    return await page.evaluate(
        """
        () => {
          const bench = window.BENCH ? JSON.parse(JSON.stringify(window.BENCH)) : null;
          let debug = null;
          try {
            debug = typeof window.__dbg === 'function' ? window.__dbg() : null;
          } catch (error) {
            debug = {error: String(error)};
          }
          const controls = [...document.querySelectorAll('button, input[type=button], input[type=submit]')]
            .filter((element) => {
              const rect = element.getBoundingClientRect();
              for (let node = element; node; node = node.parentElement) {
                const style = getComputedStyle(node);
                if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) return false;
              }
              const top = document.elementFromPoint(rect.x + rect.width/2, rect.y + rect.height/2);
              return !element.disabled && rect.width > 0 && rect.height > 0
                && (top === element || element.contains(top));
            })
            .map((element) => (element.innerText || element.value || '').trim())
            .filter(Boolean);
          return {
            bench,
            phase: debug && typeof debug.phase === 'string' ? debug.phase : null,
            controls,
            bodyText: document.body.innerText.slice(0, 1200)
          };
        }
        """
    )


def bench_is_initial(state: dict[str, Any]) -> bool:
    bench = state.get("bench")
    return bool(
        isinstance(bench, dict)
        and bench.get("attempts") == 0
        and bench.get("maxAttempts") == 3
        and bench.get("passed") is False
        and bench.get("results") == []
        and bench.get("status") == "running"
    )


def has_entry_control(state: dict[str, Any]) -> bool:
    return any(
        label.strip().lower() in ENTRY_LABELS
        or re.match(r"^(start|begin|play|reveal|shuffle|spin|drop)\b|^开始", label.strip(), re.I)
        for label in state.get("controls", [])
    )


async def inspect_game(
    browser,
    semaphore: asyncio.Semaphore,
    base_url: str,
    benchmark_id: str,
    repetitions: int,
    settle_seconds: float,
    artifacts: Path,
) -> dict[str, Any]:
    repetitions_report = []
    async with semaphore:
        for repetition in range(1, repetitions + 1):
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            await page.goto(
                f"{base_url}/{benchmark_id}/index.html",
                wait_until="load",
                timeout=30_000,
            )
            # Match the formal Config's 3-second settle, then check again later.
            await page.wait_for_timeout(3000)
            initial = await page_state(page)
            initial_path = artifacts / "initial" / f"{benchmark_id}-{repetition}.png"
            initial_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(initial_path), full_page=False)

            await page.wait_for_timeout(settle_seconds * 1000)
            delayed = await page_state(page)
            delayed_path = artifacts / "delayed" / f"{benchmark_id}-{repetition}.png"
            delayed_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(delayed_path), full_page=False)
            await page.close()

            phase_stable = (
                initial.get("phase") is None
                or delayed.get("phase") is None
                or initial.get("phase") == delayed.get("phase")
            )
            repetitions_report.append(
                {
                    "repetition": repetition,
                    "initial": initial,
                    "delayed": delayed,
                    "initial_bench_ok": bench_is_initial(initial),
                    "delayed_bench_ok": bench_is_initial(delayed),
                    "entry_control_visible": has_entry_control(initial),
                    "phase_stable": phase_stable,
                    "page_errors": page_errors,
                    "screenshots": {
                        "initial": str(initial_path),
                        "delayed": str(delayed_path),
                    },
                }
            )

    return {
        "benchmark_id": benchmark_id.upper(),
        "passed_automatic_checks": all(
            item["initial_bench_ok"]
            and item["delayed_bench_ok"]
            and item["entry_control_visible"]
            and item["phase_stable"]
            and not item["page_errors"]
            for item in repetitions_report
        ),
        "repetitions": repetitions_report,
    }


def create_contact_sheets(artifacts: Path, reports: list[dict[str, Any]]) -> None:
    sheet_dir = artifacts / "contact_sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    thumb_width, thumb_height = 480, 270
    label_height, columns, rows = 28, 4, 3
    per_sheet = columns * rows

    for offset in range(0, len(reports), per_sheet):
        subset = reports[offset : offset + per_sheet]
        sheet = Image.new(
            "RGB",
            (columns * thumb_width, rows * (thumb_height + label_height)),
            "white",
        )
        draw = ImageDraw.Draw(sheet)
        for index, report in enumerate(subset):
            source = Path(
                report["repetitions"][0]["screenshots"]["delayed"]
            )
            with Image.open(source) as screenshot:
                screenshot = screenshot.convert("RGB")
                screenshot.thumbnail((thumb_width, thumb_height))
                x = (index % columns) * thumb_width
                y = (index // columns) * (thumb_height + label_height)
                sheet.paste(screenshot, (x, y + label_height))
                outcome = "PASS" if report["passed_automatic_checks"] else "CHECK"
                draw.text((x + 8, y + 7), f"{report['benchmark_id']} {outcome}", fill="black")
        sheet.save(sheet_dir / f"sheet-{offset // per_sheet + 1:02d}.jpg", quality=88)


async def run(args) -> int:
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    port = available_port()
    server = subprocess.Popen(
        [
            "python3",
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(WEBSITE_ROOT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=str(args.browser),
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            semaphore = asyncio.Semaphore(args.concurrency)
            reports = await asyncio.gather(
                *[
                    inspect_game(
                        browser,
                        semaphore,
                        f"http://127.0.0.1:{port}",
                        benchmark_id,
                        args.repetitions,
                        args.settle_seconds,
                        artifacts,
                    )
                    for benchmark_id in benchmark_ids()
                ]
            )
            await browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    report_path = artifacts / "initial_state_report.json"
    report_path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    create_contact_sheets(artifacts, reports)

    review_ids = [
        item["benchmark_id"]
        for item in reports
        if not item["passed_automatic_checks"]
    ]
    print(f"Checked {len(reports)} games x {args.repetitions} independent browser loads (not Docker resets)")
    print(f"Automatic pass: {len(reports) - len(review_ids)}")
    print(f"Needs review: {len(review_ids)}")
    if review_ids:
        print("Needs review IDs: " + ", ".join(review_ids))
    print(f"Report: {report_path}")
    print("CHECK is a review signal only; it does not fail validation.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", type=Path, default=DEFAULT_BROWSER)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path("/tmp/realtime_gui_bench_validation"),
    )
    args = parser.parse_args()
    if args.repetitions < 1 or args.settle_seconds < 0 or args.concurrency < 1:
        raise SystemExit("Invalid validation arguments")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
