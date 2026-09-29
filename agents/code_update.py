"""Download a commit of the hub's code from GitHub and mark it active for the next restart."""
from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

try:
    import httpx2 as httpx
except ImportError:
    import httpx

REPO = os.environ.get("HUB_REPO", "Ye-shp/model-hub")
PARTS = ("agents/", "integrations/", "skills/", "console/", "deploy/")


def extract(archive: bytes, target: Path) -> int:
    """Unpack only the app folders, refusing links and paths that leave the target."""
    count = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.split("/", 1)[1] if "/" in member.name else ""
            if not name.startswith(PARTS) or not (member.isfile() or member.isdir()):
                continue
            destination = (target / name).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise ValueError(f"Unsafe path in archive: {member.name}")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(tar.extractfile(member).read())
            count += 1
    return count


async def stage(ref: str) -> dict:
    root = Path(os.environ.get("HUB_CODE_DIR", ""))
    if not str(root) or str(root) == ".":
        raise ValueError("HUB_CODE_DIR is not set; code updates are only available on the hub box")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(f"https://codeload.github.com/{REPO}/tar.gz/{ref}")
        response.raise_for_status()
    if len(response.content) > 80_000_000:
        raise ValueError("Archive is unexpectedly large")
    temporary = root / f"{ref}.tmp"
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    files = extract(response.content, temporary)
    if not (temporary / "agents" / "console.py").is_file():
        raise ValueError("That commit doesn't contain the hub's agents")
    final = root / ref
    shutil.rmtree(final, ignore_errors=True)
    temporary.rename(final)
    (root / "active").write_text(ref)
    for old in root.iterdir():  # keep this one and the previous few
        if old.is_dir() and old.name != ref and len(list(root.iterdir())) > 5:
            shutil.rmtree(old, ignore_errors=True)
    return {"staged": ref, "files": files, "next": "restart the instance (not recycle) to run it"}
