from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

from recover_xiaomi_pdf_images import (
    MEDIAFIRE_PAGE,
    extract_images,
    http,
    mediafire_direct,
    save_images,
    update_report,
)

PASSWORD = "www.sumatechsolution.com"


def recover_pdf_with_unar() -> bytes:
    client = http()
    direct = mediafire_direct(client)
    response = client.get(direct, headers={"Referer": MEDIAFIRE_PAGE}, timeout=150, allow_redirects=True)
    response.raise_for_status()
    archive_data = response.content
    unar = shutil.which("unar")
    if not unar:
        raise RuntimeError("unar is unavailable")

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        archive = base / "xiaomi-test-point.rar"
        output = base / "out"
        archive.write_bytes(archive_data)
        commands = [
            [unar, "-force-overwrite", "-output-directory", str(output), str(archive)],
            [unar, "-force-overwrite", "-password", PASSWORD, "-output-directory", str(output), str(archive)],
        ]
        errors: list[str] = []
        for command in commands:
            shutil.rmtree(output, ignore_errors=True)
            output.mkdir()
            process = subprocess.run(command, capture_output=True, text=True, timeout=180)
            pdfs = list(output.rglob("*.pdf"))
            if process.returncode == 0 and len(pdfs) == 1:
                return pdfs[0].read_bytes()
            errors.append((process.stderr or process.stdout or "unknown unar failure")[-500:])
        raise RuntimeError(" | ".join(errors))


def main() -> int:
    pdf_data = recover_pdf_with_unar()
    images = extract_images(pdf_data)
    saved, skipped = save_images(images)
    update_report(saved, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
