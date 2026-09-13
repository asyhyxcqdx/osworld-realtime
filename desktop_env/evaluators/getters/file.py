import os
import io
import logging
import time
import uuid
import zipfile
import shutil
from typing import Dict, List, Set
from typing import Optional, Any, Union
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
import requests
import pandas as pd

logger = logging.getLogger("desktopenv.getter.file")

_ZIP_DOCUMENT_EXTENSIONS = frozenset({
    ".docx",
    ".odp",
    ".ods",
    ".odt",
    ".pptx",
    ".xlsx",
})
_VM_FILE_FETCH_ATTEMPTS = 3
_VM_FILE_FETCH_RETRY_INTERVAL = 1


def _is_complete_document(file_data: bytes, path: str) -> bool:
    """Reject Office/OpenDocument snapshots captured while an app is saving."""
    if os.path.splitext(path)[1].lower() not in _ZIP_DOCUMENT_EXTENSIONS:
        return True

    try:
        with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
            archive.infolist()
        return True
    except (OSError, zipfile.BadZipFile):
        return False


def _fetch_complete_vm_file(env, path: str) -> Optional[bytes]:
    for attempt in range(1, _VM_FILE_FETCH_ATTEMPTS + 1):
        file_data = env.controller.get_file(path)
        if file_data is None:
            return None
        if _is_complete_document(file_data, path):
            return file_data

        logger.warning(
            "Fetched an incomplete archive from VM: %s (attempt %d/%d)",
            path,
            attempt,
            _VM_FILE_FETCH_ATTEMPTS,
        )
        if attempt < _VM_FILE_FETCH_ATTEMPTS:
            time.sleep(_VM_FILE_FETCH_RETRY_INTERVAL)

    logger.error(
        "Failed to fetch a complete archive from VM after %d attempts: %s",
        _VM_FILE_FETCH_ATTEMPTS,
        path,
    )
    return None


def _resolve_huggingface_url(url: str) -> str:
    endpoint = os.getenv("HF_ENDPOINT", "").strip()
    source = urlsplit(url)
    target = urlsplit(endpoint)
    if source.hostname not in {"huggingface.co", "www.huggingface.co"}:
        return url
    if not target.scheme or not target.netloc:
        return url

    target_path = f"{target.path.rstrip('/')}{source.path}"
    return urlunsplit(
        (target.scheme, target.netloc, target_path, source.query, source.fragment)
    )


def get_content_from_vm_file(env, config: Dict[str, Any]) -> Any:
    """
    Config:
        path (str): absolute path on the VM to fetch
    """

    path = config["path"]
    file_path = get_vm_file(env, {"path": path, "dest": os.path.basename(path)})
    file_type, file_content = config['file_type'], config['file_content']
    if file_type == 'xlsx':
        if file_content == 'last_row':
            df = pd.read_excel(file_path)
            last_row = df.iloc[-1]
            last_row_as_list = last_row.astype(str).tolist()
            return last_row_as_list
    else:
        raise NotImplementedError(f"File type {file_type} not supported")



def get_local_file(env, config: Dict[str, Any]) -> Any:
    """Copy an evaluator fixture that already lives in the local repo into cache."""
    path = config["path"]
    dest = os.path.join(env.cache_dir, config.get("dest", os.path.basename(path)))
    if not os.path.exists(path):
        logger.error(f"[ERROR]: The specified local file path {path} was not found")
        return None
    if os.path.isdir(path):
        if os.path.exists(dest) and os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(path, dest, dirs_exist_ok=True)
    else:
        shutil.copyfile(path, dest)
    return dest

def get_cloud_file(env, config: Dict[str, Any]) -> Union[str, List[str]]:
    """
    Config:
        path (str|List[str]): the url to download from
        dest (str|List[str])): file name of the downloaded file
        multi (bool) : optional. if path and dest are lists providing
          information of multiple files. defaults to False
        gives (List[int]): optional. defaults to [0]. which files are directly
          returned to the metric. if len==1, str is returned; else, list is
          returned.
    """

    if not config.get("multi", False):
        paths: List[str] = [config["path"]]
        dests: List[str] = [config["dest"]]
    else:
        paths: List[str] = config["path"]
        dests: List[str] = config["dest"]
    cache_paths: List[str] = []

    gives: Set[int] = set(config.get("gives", [0]))

    for i, (p, d) in enumerate(zip(paths, dests)):
        _path = os.path.join(env.cache_dir, d)
        if i in gives:
            cache_paths.append(_path)

        if os.path.exists(_path):
            #return _path
            continue

        url = _resolve_huggingface_url(p)
        response = requests.get(url, stream=True, timeout=300)
        response.raise_for_status()

        # Atomic write: stream into a temp file then rename, so a concurrent
        # reader never observes a partially-downloaded file.
        tmp_path = f"{_path}.tmp.{uuid.uuid4().hex}"
        try:
            with open(tmp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp_path, _path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    return cache_paths[0] if len(cache_paths)==1 else cache_paths


def get_vm_file(env, config: Dict[str, Any]) -> Union[Optional[str], List[Optional[str]]]:
    """
    Config:
        path (str): absolute path on the VM to fetch
        dest (str): file name of the downloaded file
        multi (bool) : optional. if path and dest are lists providing
          information of multiple files. defaults to False
        gives (List[int]): optional. defaults to [0]. which files are directly
          returned to the metric. if len==1, str is returned; else, list is
          returned.
        only support for single file now:
        time_suffix(bool): optional. defaults to False. if True, append the current time in required format.
        time_format(str): optional. defaults to "%Y%m%d_%H%M%S". format of the time suffix.
    """
    time_format = "%Y%m%d_%H%M%S"
    if not config.get("multi", False):
        paths: List[str] = [config["path"]]
        dests: List[str] = [config["dest"]]
        if config.get("time_suffix", False):
            time_format = config.get("time_format", time_format)
            # Insert time before file extension.
            dests = [f"{os.path.splitext(d)[0]}_{datetime.now().strftime(time_format)}{os.path.splitext(d)[1]}" for d in dests]
    else:
        paths: List[str] = config["path"]
        dests: List[str] = config["dest"]


    cache_paths: List[str] = []

    gives: Set[int] = set(config.get("gives", [0]))

    for i, (p, d) in enumerate(zip(paths, dests)):
        _path = os.path.join(env.cache_dir, d)

        try:
            # Try to get file from VM
            file = _fetch_complete_vm_file(env, p)
            if file is None:
                logger.warning(f"Failed to get file from VM: {p}")
                if i in gives:
                    cache_paths.append(None)
                continue

            if i in gives:
                cache_paths.append(_path)

            # Write file with robust error handling
            try:
                # Ensure cache directory exists
                os.makedirs(env.cache_dir, exist_ok=True)

                tmp_path = f"{_path}.tmp.{uuid.uuid4().hex}"
                try:
                    with open(tmp_path, "wb") as f:
                        f.write(file)
                    os.replace(tmp_path, _path)
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                logger.info(f"Successfully saved file: {_path} ({len(file)} bytes)")

            except IOError as e:
                logger.error(f"IO error writing file {_path}: {e}")
                if i in gives:
                    cache_paths[-1] = None  # Replace the path we just added with None
            except Exception as e:
                logger.error(f"Unexpected error writing file {_path}: {e}")
                if i in gives:
                    cache_paths[-1] = None

        except Exception as e:
            logger.error(f"Error processing file {p}: {e}")
            if i in gives:
                cache_paths.append(None)

    return cache_paths[0] if len(cache_paths)==1 else cache_paths


def get_cache_file(env, config: Dict[str, str]) -> str:
    """
    Config:
        path (str): relative path in cache dir
    """

    _path = os.path.join(env.cache_dir, config["path"])
    assert os.path.exists(_path)
    return _path
