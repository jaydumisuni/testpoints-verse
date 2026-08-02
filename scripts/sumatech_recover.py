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
ARCHIVE_PASSWORD = "www.sumatechsolution.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
LOCK = threading.Lock()


def new_session() -> requests.Session:
    value = requests.Session()
    value.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    })
    return value


def clean_name(value: str) -> str:
    value = unquote(value or "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\.(?:rar|zip|7z|jpe?g|png|webp|bmp)$", "", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9.+()]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-._ ")[:180] or "unknown-device"


def load_report() -> dict:
    if REPORT_PATH.exists():
        try:
            return json.loads(REPORT_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"sources": {}}


def write_report(payload: dict) -> None:
    value = load_report()
    value.setdefault("sources", {})["sumatech"] = payload
    REPORT_PATH.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", "utf-8")


def mediafire_direct(page_url: str, client: requests.Session) -> str:
    response = client.get(page_url, timeout=60, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    button = soup.select_one("a#downloadButton[href], a.input.popsok[href], a[aria-label*='Download'][href]")
    if not button or not button.get("href"):
        raise RuntimeError("MediaFire direct-download button not found")
    direct = urljoin(response.url, button.get("href"))
    if direct.startswith("javascript:") or direct.endswith("#"):
        raise RuntimeError("MediaFire returned a non-download button")
    return direct


def classify(label: str, inner_name: str) -> tuple[Path, str]:
    combined = f"{label} {inner_name}"
    is_isp = bool(re.search(r"\b(?:isp|ips|pinout|emmc|ufs)\b", combined, re.I))
    if not is_isp:
        return ROOT / "test-points" / "sumatech", "test-point"
    protocol = "ufs" if re.search(r"\bufs\b", combined, re.I) else "emmc" if re.search(r"\bemmc\b", combined, re.I) else "unclassified"
    return ROOT / "isp-pinouts" / protocol / "sumatech", protocol


def save_webp(data: bytes, directory: Path, title: str) -> str:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 80 or image.height < 80:
            raise ValueError(f"image too small: {image.width}x{image.height}")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=84, method=6)
        output = buffer.getvalue()

    directory.mkdir(parents=True, exist_ok=True)
    stem = clean_name(title)
    digest = hashlib.sha256(output).hexdigest()
    with LOCK:
        target = directory / f"{stem}.webp"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                raise FileExistsError("already saved")
            target = directory / f"{stem}-{digest[:8]}.webp"
        target.write_bytes(output)
    return str(target.relative_to(ROOT))


def extracted_images(archive_data: bytes) -> list[Path]:
    sevenzip = shutil.which("7z") or shutil.which("7zz")
    unar = shutil.which("unar")
    if not sevenzip and not unar:
        raise RuntimeError("neither 7z nor unar is available")

    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        archive = temp_path / "download.bin"
        archive.write_bytes(archive_data)
        output = temp_path / "out"
        attempts: list[list[str]] = []
        if sevenzip:
            attempts.extend([
                [sevenzip, "x", "-y", f"-p{ARCHIVE_PASSWORD}", f"-o{output}", str(archive)],
                [sevenzip, "x", "-y", f"-o{output}", str(archive)],
            ])
        if unar:
            attempts.extend([
                [unar, "-force-overwrite", "-password", ARCHIVE_PASSWORD, "-output-directory", str(output), str(archive)],
                [unar, "-force-overwrite", "-output-directory", str(output), str(archive)],
            ])

        errors: list[str] = []
        for command in attempts:
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
                copied: list[Path] = []
                durable = temp_path / "images"
                durable.mkdir(exist_ok=True)
                for index, image in enumerate(images, 1):
                    target = durable / f"{index:03d}{image.suffix.lower()}"
                    target.write_bytes(image.read_bytes())
                    copied.append(target)
                # Return bytes-backed temporary paths only inside this function is unsafe,
                # so callers consume them before the context closes through the helper below.
                return [Path(f"{path.name}|{path.read_bytes().hex()}") for path in copied]
            errors.append((process.stderr or process.stdout or "unknown extraction failure")[-400:])
        raise RuntimeError("archive extraction failed: " + " | ".join(errors[-4:]))


def decode_extracted(items: list[Path]) -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    for item in items:
        name, encoded = str(item).split("|", 1)
        result.append((name, bytes.fromhex(encoded)))
    return result


def process_package(label: str, page_url: str) -> tuple[int, int, list[str]]:
    client = new_session()
    direct = mediafire_direct(page_url, client)
    response = client.get(direct, headers={"Referer": page_url}, timeout=120, allow_redirects=True)
    response.raise_for_status()
    data = response.content
    ctype = (response.headers.get("content-type") or "").lower()
    saved = skipped = 0
    paths: list[str] = []

    if ctype.startswith("image/"):
        directory, _ = classify(label, label)
        try:
            paths.append(save_webp(data, directory, label))
            saved += 1
        except FileExistsError:
            skipped += 1
        return saved, skipped, paths

    extracted = decode_extracted(extracted_images(data))
    for index, (inner_name, image_data) in enumerate(extracted, 1):
        directory, _ = classify(label, inner_name)
        title = label if len(extracted) == 1 else f"{label}-{index}"
        try:
            paths.append(save_webp(image_data, directory, title))
            saved += 1
        except FileExistsError:
            skipped += 1
    return saved, skipped, paths


def main() -> int:
    client = new_session()
    response = client.get(INDEX_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    unique: dict[str, str] = {}
    for anchor in soup.select("a[href*='mediafire.com']"):
        href = anchor.get("href")
        if not href:
            continue
        label = anchor.get_text(" ", strip=True) or Path(href).name or "sumatech-device"
        unique[urljoin(INDEX_URL, href)] = label

    packages = [(label, href) for href, label in unique.items()]
    saved = skipped = failed = 0
    notes: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(process_package, label, href): (label, href) for label, href in packages}
        for future in as_completed(futures):
            label, href = futures[future]
            try:
                new_count, old_count, paths = future.result()
                saved += new_count
                skipped += old_count
                for path in paths:
                    print(f"SAVED {path}", flush=True)
            except Exception as exc:
                failed += 1
                if len(notes) < 160:
                    notes.append(f"{label}: {type(exc).__name__}: {exc} [{href}]")
                print(f"FAILED {label}: {exc}", flush=True)

    status = "success" if packages and failed == 0 else "partial" if saved + skipped else "failed"
    write_report({
        "status": status,
        "links": len(packages),
        "saved": saved,
        "skipped_existing": skipped,
        "failed": failed,
        "archive_password_used": ARCHIVE_PASSWORD,
        "notes": notes,
    })
    return 0 if saved + skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
