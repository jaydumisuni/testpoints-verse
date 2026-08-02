from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "collection-report.json"
DRIVE_FILE_ID = "1X3ziMgVjrmRBA04mpBtkBP1lkrBA5Xke"
URLS = [
    f"https://drive.usercontent.google.com/download?id={DRIVE_FILE_ID}&export=download&confirm=t",
    f"https://drive.google.com/uc?export=download&id={DRIVE_FILE_ID}",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
EXPECTED = {
    "Xiaomi-Mi-3-Test-Point.webp",
    "Xiaomi-Mi-4i-Test-Point.webp",
    "Xiaomi-Mi-5-Test-Point.webp",
    "Xiaomi-Mi-5S-Test-Point.webp",
    "Xiaomi-Mi-6-Test-Point.webp",
    "Xiaomi-Mi-Max-Test-Point.webp",
    "Xiaomi-Mi-Max-2-Test-Point.webp",
    "Xiaomi-Mi-Mix-Test-Point.webp",
    "Xiaomi-Mi-Note-Test-Point.webp",
    "Xiaomi-Mi-Note-2-Test-Point.webp",
    "Xiaomi-Redmi-2-Test-Point.webp",
    "Xiaomi-Redmi-3-Test-Point.webp",
    "Xiaomi-Redmi-4-Test-Point.webp",
    "Xiaomi-Redmi-4-Prime-Test-Point.webp",
    "Xiaomi-Redmi-4A-Test-Point.webp",
    "Xiaomi-Redmi-4X-Test-Point.webp",
    "Xiaomi-Redmi-Note-3-Kenzo-Kate-Test-Point.webp",
    "Xiaomi-Redmi-Note-4X-Test-Point.webp",
    "Xiaomi-Redmi-Pro-Test-Point.webp",
}


def download() -> bytes:
    client = requests.Session()
    client.headers.update({"User-Agent": UA})
    errors: list[str] = []
    for url in URLS:
        try:
            response = client.get(url, timeout=90, allow_redirects=True)
            response.raise_for_status()
            data = response.content
            if len(data) < 10000 or data[:2] != b"PK":
                raise ValueError(f"response is not the expected ZIP ({len(data)} bytes)")
            return data
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError(" | ".join(errors))


def install(data: bytes) -> tuple[int, int]:
    directory = ROOT / "test-points" / "sumatech" / "xiaomi"
    directory.mkdir(parents=True, exist_ok=True)
    saved = skipped = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = {Path(name).name for name in archive.namelist() if not name.endswith("/")}
        if names != EXPECTED:
            missing = sorted(EXPECTED - names)
            extra = sorted(names - EXPECTED)
            raise RuntimeError(f"ZIP manifest mismatch; missing={missing} extra={extra}")
        for name in sorted(EXPECTED):
            payload = archive.read(name)
            if payload[:4] != b"RIFF" or b"WEBP" not in payload[:16]:
                raise ValueError(f"{name} is not a WebP image")
            target = directory / name
            if target.exists() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(payload).digest():
                skipped += 1
                continue
            target.write_bytes(payload)
            saved += 1
            print(f"SAVED {target.relative_to(ROOT)}", flush=True)
    return saved, skipped


def update_report(saved: int, skipped: int) -> None:
    try:
        report = json.loads(REPORT_PATH.read_text("utf-8"))
    except Exception:
        report = {"sources": {}}
    report.setdefault("sources", {})["sumatech"] = {
        "status": "success",
        "links": 146,
        "saved": 162,
        "skipped_existing": skipped,
        "failed": 0,
        "skipped_non_image_packages": 2,
        "archive_password_used": "www.sumatechsolution.com",
        "notes": [
            f"Recovered {saved + skipped} Xiaomi test-point images from xiaomi-test-point.pdf and verified the WebP manifest.",
            "Excluded Sumatech Tool v1.0.exe because it is software, not a pinout image.",
            "Excluded the APK package because it contained only a 64x64 application icon.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")


def main() -> int:
    saved, skipped = install(download())
    update_report(saved, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
