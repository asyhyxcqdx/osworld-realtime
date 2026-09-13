import argparse
import os
import shutil
from pathlib import Path

import requests

os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")

from huggingface_hub.file_download import xet_get
from huggingface_hub.utils._xet import XetFileData


DEFAULT_REPO = "ByteDance-Seed/UI-TARS-1.5-7B"
DEFAULT_TARGET = Path("/mnt/zhaorunsong/models/UI-TARS-1.5-7B")
DEFAULT_ENDPOINT = "https://hf-mirror.com"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download UI-TARS weight shards directly through Hugging Face Xet."
    )
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def get_json(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def main():
    args = parse_args()
    endpoint = args.endpoint.rstrip("/")
    args.target.mkdir(parents=True, exist_ok=True)

    model_info = get_json(f"{endpoint}/api/models/{args.repo}")
    revision = model_info["sha"]
    tree = get_json(
        f"{endpoint}/api/models/{args.repo}/tree/{revision}"
        "?recursive=true&expand=true"
    )
    shards = sorted(
        (
            item
            for item in tree
            if item.get("path", "").endswith(".safetensors")
            and item.get("xetHash")
        ),
        key=lambda item: item["path"],
    )
    if not shards:
        raise RuntimeError("No Xet-backed safetensors files found in the model repository.")

    total_size = sum(item["size"] for item in shards)
    existing_size = sum(
        item["size"]
        for item in shards
        if (args.target / item["path"]).is_file()
        and (args.target / item["path"]).stat().st_size == item["size"]
    )
    free_size = shutil.disk_usage(args.target).free
    remaining_size = total_size - existing_size
    if free_size < remaining_size:
        raise RuntimeError(
            f"Not enough disk space: need {remaining_size} bytes, have {free_size} bytes."
        )

    print(f"Repository revision: {revision}")
    print(f"Weight shards: {len(shards)}")
    print(f"Remaining download: {remaining_size / 1_000_000_000:.2f} GB")
    if args.dry_run:
        print("Dry run complete; no weights were downloaded.")
        return

    refresh_route = (
        f"{endpoint}/api/models/{args.repo}/xet-read-token/{revision}"
    )
    for index, item in enumerate(shards, start=1):
        relative_path = Path(item["path"])
        target_path = args.target / relative_path
        expected_size = item["size"]

        if target_path.is_file():
            if target_path.stat().st_size == expected_size:
                print(f"[{index}/{len(shards)}] Already complete: {relative_path}")
                continue
            raise RuntimeError(
                f"Existing file has the wrong size: {target_path} "
                f"({target_path.stat().st_size} != {expected_size})"
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        incomplete_path = target_path.with_name(target_path.name + ".xet.incomplete")
        print(f"[{index}/{len(shards)}] Downloading {relative_path}")
        xet_get(
            incomplete_path=incomplete_path,
            xet_file_data=XetFileData(
                file_hash=item["xetHash"],
                refresh_route=refresh_route,
            ),
            headers={},
            expected_size=expected_size,
            displayed_filename=relative_path.name,
        )
        if incomplete_path.stat().st_size != expected_size:
            raise RuntimeError(
                f"Downloaded file has the wrong size: {incomplete_path}"
            )
        incomplete_path.replace(target_path)

    print(f"\nDownload complete: {args.target}")


if __name__ == "__main__":
    main()
