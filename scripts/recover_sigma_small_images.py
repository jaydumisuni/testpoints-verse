from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "collection-report.json"
PAGE = "https://sigmakey.com/en/sigma-help/testpoints-pinouts/?brand=4"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
TARGETS = {
    "Y6 SCL-AL00-1753700338": "Huawei-Y6-SCL-AL00-Test-Point",
    "AGR-W09-1753701972": "Huawei-AGR-W09-Test-Point",
    "P8 GRA-L09-1753709181": "Huawei-P8-GRA-L09-Test-Point",
}


def session() -> requests.Session:
    client = requests.Session()
    client.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    })
    return client


def discover(client: requests.Session) -> dict[str, str]:
    response = client.get(PAGE, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found: dict[str, str] = {}
    for tag in soup.select("a[href], img[src], img[data-src]"):
        raw = tag.get("href") or tag.get("data-src") or tag.get("src")
        if not raw:
            continue
        url = urljoin(PAGE, raw)
        decoded = unquote(url)
        if "/content/nfs/testpoints/" not in decoded.lower():
            continue
        if not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", url, re.I):
            continue
        for target in TARGETS:
            if target.lower() in decoded.lower():
                found[target] = url
    return found


def save(client: requests.Session, target: str, url: str) -> str:
    response = client.get(url, headers={"Referer": PAGE}, timeout=60, allow_redirects=True)
    response.raise_for_status()
    data = response.content
    if len(data) < 500 or "text/html" in (response.headers.get("content-type") or "").lower():
        raise ValueError("image request did not return an image")
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 80 or image.height < 80:
            raise ValueError(f"image is not a usable test-point reference: {image.width}x{image.height}")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=84, method=6)
        output = buffer.getvalue()

    directory = ROOT / "test-points" / "huawei"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{TARGETS[target]}.webp"
    if path.exists() and hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(output).digest():
        return str(path.relative_to(ROOT))
    path.write_bytes(output)
    return str(path.relative_to(ROOT))


def update_report(saved: int, failures: list[str]) -> None:
    try:
        report = json.loads(REPORT_PATH.read_text("utf-8"))
    except Exception:
        report = {"sources": {}}
    report.setdefault("sources", {})["sigmakey-huawei"] = {
        "status": "success" if not failures else "partial",
        "saved": 166 + saved,
        "failed": len(failures),
        "notes": failures + ["discovered=169"],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")


def main() -> int:
    client = session()
    discovered = discover(client)
    failures: list[str] = []
    saved = 0
    for target in TARGETS:
        url = discovered.get(target)
        if not url:
            failures.append(f"{target}: direct image URL was not found in Sigma page HTML")
            continue
        try:
            print(f"SAVED {save(client, target, url)}", flush=True)
            saved += 1
        except Exception as exc:
            failures.append(f"{target}: {type(exc).__name__}: {exc}")
    update_report(saved, failures)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
