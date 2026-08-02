from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "collection-report.json"
INDEX_URL = "https://www.sumatechsolution.com/p/test-point.html"
PASSWORD = "www.sumatechsolution.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
WRITE_LOCK = threading.Lock()


def client() -> requests.Session:
    value = requests.Session()
    value.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    })
    return value


def clean(value: str) -> str:
    value = unquote(value or "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\.(?:rar|zip|7z|jpe?g|png|webp|bmp)$", "", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9.+()]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-._ ")[:180] or "unknown-device"


def merge_report(payload: dict) -> None:
    try:
        report = json.loads(REPORT_PATH.read_text("utf-8"))
    except Exception:
        report = {"sources": {}}
    report.setdefault("sources", {})["sumatech"] = payload
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")


def direct_url(page_url: str, http: requests.Session) -> str:
    page = http.get(page_url, timeout=60, allow_redirects=True)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    button = soup.select_one("a#downloadButton[href], a.input.popsok[href], a[aria-label*='Download'][href]")
    if not button or not button.get("href"):
        raise RuntimeError("MediaFire direct-download button not found")
    result = urljoin(page.url, button.get("href"))
    if result.startswith("javascript:") or result.endswith("#"):
        raise RuntimeError("MediaFire returned a non-download link")
    return result


def extract_images(data: bytes) -> list[tuple[str, bytes]]:
    sevenzip = shutil.which("7z") or shutil.which("7zz")
    unar = shutil.which("unar")
    if not sevenzip and not unar:
        raise RuntimeError("7z and unar are unavailable")

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        archive = base / "download.bin"
        archive.write_bytes(data)
        output = base / "out"
        commands: list[list[str]] = []
        if sevenzip:
            commands += [
                [sevenzip, "x", "-y", f"-p{PASSWORD}", f"-o{output}", str(archive)],
                [sevenzip, "x", "-y", f"-o{output}", str(archive)],
            ]
        if unar:
            commands += [
                [unar, "-force-overwrite", "-password", PASSWORD, "-output-directory", str(output), str(archive)],
                [unar, "-force-overwrite", "-output-directory", str(output), str(archive)],
            ]

        errors: list[str] = []
        for command in commands:
            shutil.rmtree(output, ignore_errors=True)
            output.mkdir(parents=True)
            try:
                process = subprocess.run(command, capture_output=True, text=True, timeout=180)
            except Exception as exc:
                errors.append(f"{Path(command[0]).name}: {type(exc).__name__}: {exc}")
                continue
            images = [
                path for path in output.rglob("*")
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
            ]
            if process.returncode == 0 and images:
                return [(image.name, image.read_bytes()) for image in images]
            errors.append((process.stderr or process.stdout or "unknown extraction failure")[-350:])
        raise RuntimeError("archive extraction failed: " + " | ".join(errors[-4:]))


def destination(label: str, inner_name: str) -> Path:
    text = f"{label} {inner_name}"
    if not re.search(r"\b(?:isp|ips|pinout|emmc|ufs)\b", text, re.I):
        return ROOT / "test-points" / "sumatech"
    protocol = "ufs" if re.search(r"\bufs\b", text, re.I) else "emmc" if re.search(r"\bemmc\b", text, re.I) else "unclassified"
    return ROOT / "isp-pinouts" / protocol / "sumatech"


def save_image(data: bytes, folder: Path, title: str) -> str:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 80 or image.height < 80:
            raise ValueError(f"image too small: {image.width}x{image.height}")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        output = io.BytesIO()
        image.save(output, "WEBP", quality=84, method=6)
        webp = output.getvalue()

    folder.mkdir(parents=True, exist_ok=True)
    stem = clean(title)
    digest = hashlib.sha256(webp).hexdigest()
    with WRITE_LOCK:
        path = folder / f"{stem}.webp"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                raise FileExistsError("already saved")
            path = folder / f"{stem}-{digest[:8]}.webp"
        path.write_bytes(webp)
    return str(path.relative_to(ROOT))


def process(label: str, mediafire_page: str) -> tuple[int, int, list[str]]:
    http = client()
    download = direct_url(mediafire_page, http)
    response = http.get(download, headers={"Referer": mediafire_page}, timeout=150, allow_redirects=True)
    response.raise_for_status()
    payload = response.content
    ctype = (response.headers.get("content-type") or "").lower()
    images = [(label, payload)] if ctype.startswith("image/") else extract_images(payload)

    saved = skipped = 0
    paths: list[str] = []
    for number, (inner_name, image_data) in enumerate(images, 1):
        title = label if len(images) == 1 else f"{label}-{number}"
        try:
            paths.append(save_image(image_data, destination(label, inner_name), title))
            saved += 1
        except FileExistsError:
            skipped += 1
    return saved, skipped, paths


def main() -> int:
    http = client()
    index = http.get(INDEX_URL, timeout=60)
    index.raise_for_status()
    soup = BeautifulSoup(index.text, "html.parser")
    links: dict[str, str] = {}
    for anchor in soup.select("a[href*='mediafire.com']"):
        href = anchor.get("href")
        if href:
            links[urljoin(INDEX_URL, href)] = anchor.get_text(" ", strip=True) or Path(href).name

    saved = skipped = failed = 0
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(process, label, url): (label, url) for url, label in links.items()}
        for future in as_completed(futures):
            label, url = futures[future]
            try:
                new, old, paths = future.result()
                saved += new
                skipped += old
                for path in paths:
                    print(f"SAVED {path}", flush=True)
            except Exception as exc:
                failed += 1
                if len(notes) < 180:
                    notes.append(f"{label}: {type(exc).__name__}: {exc} [{url}]")
                print(f"FAILED {label}: {exc}", flush=True)

    status = "success" if links and failed == 0 else "partial" if saved + skipped else "failed"
    merge_report({
        "status": status,
        "links": len(links),
        "saved": saved,
        "skipped_existing": skipped,
        "failed": failed,
        "archive_password_used": PASSWORD,
        "notes": notes,
    })
    return 0 if saved + skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
