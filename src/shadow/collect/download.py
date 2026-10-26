import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

USER_AGENT = "shadow-monitor/0.1 (research project)"


def download(url: str, path: Path, max_age: timedelta) -> Path:
    """Download url to path unless a cached copy younger than max_age exists."""
    if path.exists() and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < max_age:
        log.info("Using cached %s", path.name)
        return path

    log.info("Downloading %s", url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with requests.get(url, stream=True, timeout=(10, 300), headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(path)
    return path
