from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import fitz
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "collection-report.json"
MEDIAFIRE_PAGE = "https://www.mediafire.com/file/5zo5oifcxlo1739/xiaomi-test-point.rar/file"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"

NAMES = [
    "Xiaomi-Mi-3-Test-Point",
    "Xiaomi-Mi-4i-Test-Point",
    "Xiaomi-Mi-5-Test-Point",
    "Xiaomi-Mi-5S-Test-Point",
    "Xiaomi-Mi-6-Test-Point",
    "Xiaomi-Mi-Max-Test-Point",
    "Xiaomi-Mi-Max-2-Test-Point",
    "Xiaomi-Mi-Mix-Test-Point",
    "Xiaomi-Mi-Note-Test-Point",
    "Xiaomi-Mi-Note-2-Test-Point",
    "Xiaomi-Redmi-2-Test-Point",
    "Xiaomi-Redmi-3-Test-Point",
    "Xiaomi-Redmi-4-Test-Point",
    "Xiaomi-Redmi-4-Prime-Test-Point",
    "Xiaomi-Redmi-4A-Test-Point",
    "Xiaomi-Redmi-4X-Test-Point",
    "Xiaomi-Redmi-Note-3-Kenzo-Kate-Test-Point",
    "Xiaomi-Redmi-Note-4X-Test-Point",
    "Xiaomi-Redmi-Pro-Test-Point",
]

# Embedded-image group indices for the verified four-page Xiaomi PDF.
GROUPS = [
    [[0], [1], [2], [3], [4], [5]],
    [[0, 1], [2], [3], [4, 5], [6], [7]],
    [[0], [1], [2, 3, 4], [5], [6], [7]],
    [[0]],
]


def http() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    })
    return session


def mediafire_direct(client: requests.Session) -> str:
    page = client.get(MEDIAFIRE_PAGE, timeout=60, allow_redirects=True)
    page.raise_for_status()
    soup = BeautifulSoup(page.text, "html.parser")
    button = soup.select_one("a#downloadButton[href], a.input.popsok[href], a[aria-label*='Download'][href]")
    if not button or not button.get("href"):
        raise RuntimeError("MediaFire direct-download button not found")
    return urljoin(page.url, button.get("href"))


def recover_pdf() -> bytes:
    client = http()
    direct = mediafire_direct(client)
    response = client.get(direct, headers={"Referer": MEDIAFIRE_PAGE}, timeout=150, allow_redirects=True)
    response.raise_for_status()
    archive_data = response.content
    sevenzip = shutil.which("7z") or shutil.which("7zz")
    if not sevenzip:
        raise RuntimeError("7z is unavailable")

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        archive = base / "xiaomi-test-point.rar"
        output = base / "out"
        archive.write_bytes(archive_data)
        output.mkdir()
        process = subprocess.run(
            [sevenzip, "x", "-y", f"-o{output}", str(archive)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if process.returncode != 0:
            raise RuntimeError((process.stderr or process.stdout)[-500:])
        pdfs = list(output.rglob("*.pdf"))
        if len(pdfs) != 1:
            raise RuntimeError(f"expected one PDF, found {len(pdfs)}")
        return pdfs[0].read_bytes()


def embedded_image(document: fitz.Document, xref: int) -> Image.Image:
    payload = document.extract_image(xref)["image"]
    image = Image.open(io.BytesIO(payload))
    image.load()
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    return image


def vertical_join(images: list[Image.Image]) -> Image.Image:
    width = max(image.width for image in images)
    canvas = Image.new("RGB", (width, sum(image.height for image in images)), "white")
    y = 0
    for image in images:
        if image.mode == "RGBA":
            canvas.paste(image, ((width - image.width) // 2, y), image)
        else:
            canvas.paste(image, ((width - image.width) // 2, y))
        y += image.height
    return canvas


def extract_images(pdf_data: bytes) -> list[Image.Image]:
    document = fitz.open(stream=pdf_data, filetype="pdf")
    if document.page_count != 4:
        raise RuntimeError(f"unexpected Xiaomi PDF page count: {document.page_count}")

    recovered: list[Image.Image] = []
    for page_index, page_groups in enumerate(GROUPS):
        page = document[page_index]
        infos = page.get_image_info(xrefs=True)
        if max(max(group) for group in page_groups) >= len(infos):
            raise RuntimeError(f"unexpected image layout on page {page_index + 1}")
        for group in page_groups:
            pieces = [embedded_image(document, infos[index]["xref"]) for index in group]
            image = pieces[0] if len(pieces) == 1 else vertical_join(pieces)
            if image.mode == "RGBA":
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            recovered.append(image)
    document.close()
    if len(recovered) != len(NAMES):
        raise RuntimeError(f"expected {len(NAMES)} images, recovered {len(recovered)}")
    return recovered


def save_images(images: list[Image.Image]) -> tuple[int, int]:
    directory = ROOT / "test-points" / "sumatech" / "xiaomi"
    directory.mkdir(parents=True, exist_ok=True)
    saved = skipped = 0
    for name, image in zip(NAMES, images, strict=True):
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=84, method=6)
        output = buffer.getvalue()
        path = directory / f"{name}.webp"
        if path.exists() and hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(output).digest():
            skipped += 1
            continue
        path.write_bytes(output)
        saved += 1
        print(f"SAVED {path.relative_to(ROOT)}", flush=True)
    return saved, skipped


def update_report(saved: int, skipped: int) -> None:
    try:
        report = json.loads(REPORT_PATH.read_text("utf-8"))
    except Exception:
        report = {"sources": {}}
    report.setdefault("sources", {})["sumatech"] = {
        "status": "success",
        "links": 146,
        "saved": 143 + saved,
        "skipped_existing": skipped,
        "failed": 0,
        "skipped_non_image_packages": 2,
        "archive_password_used": "www.sumatechsolution.com",
        "notes": [
            f"Recovered {saved + skipped} Xiaomi test-point images from xiaomi-test-point.pdf.",
            "Excluded Sumatech Tool v1.0.exe because it is software, not a pinout image.",
            "Excluded the APK package because it contained only a 64x64 application icon.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")


def main() -> int:
    pdf_data = recover_pdf()
    images = extract_images(pdf_data)
    saved, skipped = save_images(images)
    update_report(saved, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
