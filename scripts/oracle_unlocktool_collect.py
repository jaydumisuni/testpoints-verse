from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile
from playwright.async_api import async_playwright

ImageFile.LOAD_TRUNCATED_IMAGES = True
ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "collection-report.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
})
TIMEOUT = 45

# The linked Drive folder is a seed/coverage set taken from UnlockTool. These
# names are checked against the complete site crawl; the browser collector is
# still authoritative and is expected to recover the entire catalogue.
DRIVE_SEEDS = [
    "Samsung A05 - A055F Pinout", "samsung A20s SM-A207F", "P30 PRO VOG-AL00 test point",
    "Nokia 2 TA-1029", "Nokia G11 Testpoint", "MOTOROLA G8 POWER LITE - XT2055-1 Testpoint",
    "Motorola Moto E13 XT2345 TestPoint", "SM-A045F", "Samsung A04E SM-A042F Test-Point",
    "Samsung A03S (A037F) Test Point", "Nokia C21 Plus Test point TA-1433", "Samsung A125F TestPoint",
    "Samsung M11 SM-M115F Test Point", "Samsung A03 Core - A032F", "Samsung A03 SM-A035F TestPoint",
    "Huawei NZONE S7 Pro 5G (COCO-AN00)", "Samsung M11 Testpoint", "Nokia 2.4 TA-1270",
    "Nokia G10 TA-1346", "Samsung A32 5G SM-A326B TP", "Samsung M02 M022F Test Point",
    "Huawei Y7 2018", "Samsung A32 4G - SM-A325F Test Point", "Huawei Mate 9 HMA-AL00 HMA-L29",
    "Samsung A02s (A025F) Test Point EDL", "Samsung A80 SM-A805F TP", "Samsung A11 (A115) TP",
    "Samsung A12 (A125F A125M A125N) TP", "Samsung A02 (A022F A022G A022M) TP",
    "Xiaomi A1 tissot TestPoint", "Huawei Mate 30 Pro 5G LIO-AL00 TestPoint",
    "Huawei Y9 2018 FLA-LX2 Kirin 659", "Nokia 4.2 TA-1157", "Nokia 6.1 Plus TA-1083",
    "Nokia 7.1 Plus TA-1131", "Mate 20 Testpoint", "Huawei Y7 Pro 2018 DUB-LX2 testpoint",
    "Honor 20 Lite (LRA) Test point", "Mate 20 Lite (SNE-AL00) Test point", "Huawei Mate 20 lite",
    "Huawei Mate 20 Pro", "Huawei Nova 3 PAR-AL00", "Huawei Mate10(ALP)", "Huawei Mate 9Pro",
    "Huawei Nova 3I (INE-LX2)", "Honor 7X (BND-L21)", "Huawei Mate 10 Pro (BLA-L09)",
    "Xiaomi Mi A2 TestPoint", "Huawei Y7 (Prime)", "Xiaomi Mi A2 Lite (Daisy) TestPoint",
    "Xiaomi Redmi S2 (Ysl) Testpoint", "Huawei Mate 8",
    "Honor 7 PLK-L01, PLK-AL10, PLK-UL00, PLK-TL01H, PLK-TL00",
]

UNLOCK_BASE = "https://edl.unlocktool.net/"
UNLOCK_CATEGORIES = {
    "asus", "huawei", "itel", "levono", "lg", "meizu", "nokia", "oppo",
    "other", "samsung", "vivo", "vsmart", "xiaomi",
}
RESERVED = UNLOCK_CATEGORIES | {
    "", "home", "assets", "uploads", "api", "search", "sitemap.xml", "robots.txt",
}


def report() -> dict:
    if REPORT_PATH.exists():
        try:
            return json.loads(REPORT_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"sources": {}}


def update_report(source: str, payload: dict) -> None:
    value = report()
    value.setdefault("sources", {})[source] = payload
    REPORT_PATH.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", "utf-8")


def normalize(value: str) -> str:
    value = unquote(value or "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\.(?:jpe?g|png|webp|gif|bmp)$", "", value, flags=re.I)
    value = re.sub(r"\b(?:test[ -]?point|testpoint|edl|brom|bootrom|photo|image|download)\b", " ", value, flags=re.I)
    value = re.sub(r"[^A-Za-z0-9.+()]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-._ ")[:180] or "unknown-device"


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize(value).lower())


def fetch(url: str, referer: str | None = None) -> requests.Response:
    headers = {"Referer": referer} if referer else None
    response = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True, headers=headers)
    response.raise_for_status()
    return response


def actual_image_bytes(url: str, referer: str) -> bytes:
    response = fetch(url, referer)
    data = response.content
    ctype = (response.headers.get("content-type") or "").lower()
    if "text/html" in ctype or data[:64].lstrip().lower().startswith((b"<!doctype", b"<html")):
        raise ValueError("image URL returned HTML")
    if len(data) < 1000:
        raise ValueError("image payload too small")
    return data


def unique_path(directory: Path, title: str, digest: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = normalize(title)
    candidate = directory / f"{stem}.webp"
    if not candidate.exists():
        return candidate
    try:
        if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            raise FileExistsError("same file already exists")
    except OSError:
        pass
    return directory / f"{stem}-{digest[:8]}.webp"


def save_webp(data: bytes, directory: Path, title: str) -> str:
    source_digest = hashlib.sha256(data).hexdigest()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 100 or image.height < 100:
            raise ValueError(f"image too small: {image.width}x{image.height}")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=84, method=6)
        output = buffer.getvalue()
    output_digest = hashlib.sha256(output).hexdigest()
    path = unique_path(directory, title, output_digest)
    path.write_bytes(output)
    return str(path.relative_to(ROOT))


def is_post_url(raw: str) -> bool:
    try:
        parsed = urlparse(raw)
    except Exception:
        return False
    if parsed.netloc and parsed.netloc != urlparse(UNLOCK_BASE).netloc:
        return False
    path = parsed.path.strip("/")
    if not path or "/" in path or path.lower() in RESERVED:
        return False
    if re.search(r"\.(?:jpg|jpeg|png|webp|gif|css|js|xml|txt)$", path, re.I):
        return False
    return True


async def rendered_unlocktool_links() -> set[str]:
    links: set[str] = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US", user_agent=UA)
        page = await context.new_page()
        await page.goto(UNLOCK_BASE, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(1500)

        stable = 0
        last_count = 0
        for _ in range(100):
            hrefs = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
            for href in hrefs:
                if is_post_url(href):
                    links.add(urljoin(UNLOCK_BASE, href))
            if len(links) == last_count:
                stable += 1
            else:
                stable = 0
                last_count = len(links)

            buttons = page.get_by_text(re.compile(r"Load more", re.I))
            count = await buttons.count()
            clicked = False
            for index in range(count):
                button = buttons.nth(index)
                try:
                    if await button.is_visible():
                        await button.scroll_into_view_if_needed()
                        await button.click(timeout=10000)
                        await page.wait_for_timeout(900)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked or stable >= 4:
                break

        # Category pages and their rendered lists provide an independent path
        # if the main page's load-more control changes.
        for category in sorted(UNLOCK_CATEGORIES):
            try:
                await page.goto(urljoin(UNLOCK_BASE, category), wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(500)
                hrefs = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                for href in hrefs:
                    if is_post_url(href):
                        links.add(urljoin(UNLOCK_BASE, href))
            except Exception:
                continue

        await context.close()
        await browser.close()
    return links


def page_links_and_record(url: str) -> tuple[dict | None, set[str]]:
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    found_links = {
        urljoin(UNLOCK_BASE, a.get("href"))
        for a in soup.select("a[href]")
        if a.get("href") and is_post_url(urljoin(UNLOCK_BASE, a.get("href")))
    }

    candidates: list[str] = []
    for tag in soup.select("img[src], img[data-src], img[data-lazy-src], a[href]"):
        raw = tag.get("data-src") or tag.get("data-lazy-src") or tag.get("src") or tag.get("href")
        if not raw:
            continue
        absolute = urljoin(url, raw)
        if "/uploads/" in urlparse(absolute).path.lower() and re.search(r"\.(?:jpe?g|png|webp|bmp)(?:$|\?)", absolute, re.I):
            candidates.append(absolute)
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return None, found_links

    title_tag = soup.select_one("h3, h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else Path(urlparse(url).path).stem
    image_url = candidates[0]
    path_parts = [part for part in urlparse(image_url).path.split("/") if part]
    brand = path_parts[1].lower() if len(path_parts) >= 3 and path_parts[0].lower() == "uploads" else "unclassified"
    brand = normalize(brand).lower()
    isp = bool(re.search(r"\b(?:isp|ips|pinout|emmc|ufs)\b", title, re.I))
    protocol = "ufs" if re.search(r"\bufs\b", title, re.I) else "emmc" if re.search(r"\bemmc\b", title, re.I) else "unclassified"
    return {
        "url": url,
        "title": title,
        "image_url": image_url,
        "brand": brand,
        "isp": isp,
        "protocol": protocol,
    }, found_links


def collect_unlocktool() -> None:
    source = "unlocktool-edl"
    rendered = asyncio.run(rendered_unlocktool_links())
    queue = deque(sorted(rendered))
    queued = set(queue)
    records: dict[str, dict] = {}
    failures: list[str] = []

    # Each post includes an "Other Posts" list. Recursively following those
    # links closes gaps even when the load-more endpoint changes.
    while queue and len(queued) <= 900:
        batch = [queue.popleft() for _ in range(min(24, len(queue)))]
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(page_links_and_record, url): url for url in batch}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record, links = future.result()
                    if record:
                        records[url] = record
                    for link in links:
                        if link not in queued:
                            queued.add(link)
                            queue.append(link)
                except Exception as exc:
                    if len(failures) < 150:
                        failures.append(f"page {url}: {type(exc).__name__}: {exc}")

    saved = skipped = image_failed = 0
    saved_titles: list[str] = []

    def download_one(record: dict) -> tuple[str, str]:
        data = actual_image_bytes(record["image_url"], record["url"])
        if record["isp"]:
            directory = ROOT / "isp-pinouts" / record["protocol"] / "unlocktool" / record["brand"]
        else:
            directory = ROOT / "test-points" / "unlocktool" / record["brand"]
        return record["title"], save_webp(data, directory, record["title"])

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(download_one, record): record for record in records.values()}
        for future in as_completed(futures):
            record = futures[future]
            try:
                title, path = future.result()
                saved += 1
                saved_titles.append(title)
                print(f"SAVED {path}", flush=True)
            except FileExistsError:
                skipped += 1
            except Exception as exc:
                image_failed += 1
                if len(failures) < 150:
                    failures.append(f"image {record['title']}: {type(exc).__name__}: {exc}")

    recovered_keys = {key(title) for title in saved_titles}
    # Existing files also count toward seed coverage.
    for path in (ROOT / "test-points").rglob("*.webp"):
        recovered_keys.add(key(path.stem))
    for path in (ROOT / "isp-pinouts").rglob("*.webp"):
        recovered_keys.add(key(path.stem))
    missing_seeds = [title for title in DRIVE_SEEDS if key(title) not in recovered_keys]

    status = "success" if len(records) >= 550 and not missing_seeds else "partial"
    update_report(source, {
        "status": status,
        "rendered_links": len(rendered),
        "discovered_posts": len(records),
        "saved": saved,
        "skipped_existing": skipped,
        "failed": image_failed,
        "drive_seed_total": len(DRIVE_SEEDS),
        "drive_seed_missing": missing_seeds,
        "notes": failures,
    })
    if len(records) < 500:
        raise RuntimeError(f"UnlockTool crawl was incomplete: only {len(records)} posts")


async def collect_droidwin() -> None:
    source = "droidwin"
    url = "https://droidwin.com/how-to-access-edl-test-point-on-various-android-devices/"
    notes: list[str] = []
    saved = failed = 0
    items: list[tuple[str, str]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1100}, locale="en-US", user_agent=UA)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            for _ in range(45):
                body = (await page.locator("body").inner_text()).lower()
                if "request is being verified" not in body and "one moment" not in body and "just a moment" not in body:
                    break
                await page.wait_for_timeout(1000)
            body = (await page.locator("body").inner_text()).lower()
            if "request is being verified" in body or "just a moment" in body:
                raise RuntimeError("browser verification did not release the article")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
            nodes = await page.locator("article h2, article h3, article h4, article img, .entry-content h2, .entry-content h3, .entry-content h4, .entry-content img").all()
            current = "android-device"
            for node in nodes:
                tag = await node.evaluate("e => e.tagName.toLowerCase()")
                if tag in {"h2", "h3", "h4"}:
                    current = (await node.inner_text()).strip() or current
                    continue
                src = await node.get_attribute("src") or await node.get_attribute("data-src") or await node.get_attribute("data-lazy-src")
                alt = await node.get_attribute("alt") or current
                if src and not re.search(r"logo|avatar|icon|banner|advert", f"{src} {alt}", re.I):
                    items.append((urljoin(url, src), alt or current))
        except Exception as exc:
            notes.append(f"{type(exc).__name__}: {exc}")
        finally:
            await context.close()
            await browser.close()

    for image_url, title in list(dict.fromkeys(items)):
        try:
            path = save_webp(actual_image_bytes(image_url, url), ROOT / "test-points" / "droidwin", title)
            print(f"SAVED {path}", flush=True)
            saved += 1
        except Exception as exc:
            failed += 1
            notes.append(f"{title}: {type(exc).__name__}: {exc}")
    update_report(source, {"status": "success" if saved else "blocked", "saved": saved, "failed": failed, "notes": notes[:150]})


async def collect_sumatech() -> None:
    source = "sumatech"
    index_url = "https://www.sumatechsolution.com/p/test-point.html"
    notes: list[str] = []
    saved = failed = 0
    links: list[tuple[str, str]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US", user_agent=UA, accept_downloads=True)
        page = await context.new_page()
        try:
            await page.goto(index_url, wait_until="domcontentloaded", timeout=90000)
            links = await page.locator("a[href*='mediafire.com']").evaluate_all(
                "els => els.map(e => [e.innerText.trim(), e.href])"
            )
        except Exception as exc:
            notes.append(f"index: {type(exc).__name__}: {exc}")

        sevenzip = shutil.which("7z") or shutil.which("7zz")
        for index, pair in enumerate(links, 1):
            label, mediafire_url = pair
            try:
                await page.goto(mediafire_url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(900)
                button = page.locator("a#downloadButton, a.input.popsok, a[aria-label*='Download']").first
                direct = await button.get_attribute("href")
                if not direct:
                    raise RuntimeError("MediaFire direct URL unavailable")
                response = await context.request.get(direct, headers={"Referer": mediafire_url}, timeout=120000)
                if not response.ok:
                    raise RuntimeError(f"download HTTP {response.status}")
                data = await response.body()
                ctype = (response.headers.get("content-type") or "").lower()
                isp = bool(re.search(r"\b(?:isp|pinout|emmc|ufs)\b", label, re.I))
                protocol = "ufs" if re.search(r"\bufs\b", label, re.I) else "emmc" if re.search(r"\bemmc\b", label, re.I) else "unclassified"
                directory = ROOT / "isp-pinouts" / protocol / "sumatech" if isp else ROOT / "test-points" / "sumatech"
                if ctype.startswith("image/"):
                    save_webp(data, directory, label)
                    saved += 1
                else:
                    if not sevenzip:
                        raise RuntimeError("7z unavailable")
                    with tempfile.TemporaryDirectory() as temp:
                        archive = Path(temp) / "download.bin"
                        archive.write_bytes(data)
                        out = Path(temp) / "out"
                        out.mkdir()
                        proc = subprocess.run([sevenzip, "x", "-y", f"-o{out}", str(archive)], capture_output=True, text=True, timeout=180)
                        if proc.returncode != 0:
                            raise RuntimeError(f"archive extraction failed: {proc.stderr[-300:]}")
                        images = [p for p in out.rglob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}]
                        if not images:
                            raise RuntimeError("archive contains no supported image")
                        for number, image in enumerate(images, 1):
                            save_webp(image.read_bytes(), directory, label if len(images) == 1 else f"{label}-{number}")
                            saved += 1
                print(f"SAVED SUMATECH {index}/{len(links)} {label}", flush=True)
            except Exception as exc:
                failed += 1
                if len(notes) < 150:
                    notes.append(f"{label}: {type(exc).__name__}: {exc}")
        await context.close()
        await browser.close()
    update_report(source, {"status": "success" if saved else "failed", "links": len(links), "saved": saved, "failed": failed, "notes": notes})


def main() -> int:
    selected = os.environ.get("SOURCE", "all").lower()
    failures = 0
    if selected in {"all", "unlocktool"}:
        try:
            collect_unlocktool()
        except Exception as exc:
            print(f"UNLOCKTOOL FAILED: {exc}", flush=True)
            failures += 1
    if selected in {"all", "droidwin"}:
        try:
            asyncio.run(collect_droidwin())
        except Exception as exc:
            print(f"DROIDWIN FAILED: {exc}", flush=True)
            failures += 1
    if selected in {"all", "sumatech"}:
        try:
            asyncio.run(collect_sumatech())
        except Exception as exc:
            print(f"SUMATECH FAILED: {exc}", flush=True)
            failures += 1
    return 1 if failures and selected != "all" else 0


if __name__ == "__main__":
    raise SystemExit(main())
