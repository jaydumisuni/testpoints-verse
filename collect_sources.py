from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "collection-report.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"})
TIMEOUT = 35


def load_report() -> dict:
    if REPORT.exists():
        try:
            return json.loads(REPORT.read_text("utf-8"))
        except Exception:
            pass
    return {"sources": {}}


def save_report(report: dict) -> None:
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", "utf-8")


def update_report(source: str, status: str, saved: int, failed: int, notes: list[str]) -> None:
    report = load_report()
    report.setdefault("sources", {})[source] = {"status": status, "saved": saved, "failed": failed, "notes": notes[:100]}
    save_report(report)


def clean_name(value: str, fallback: str = "unknown-device") -> str:
    value = unquote(value or "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\.(?:jpe?g|png|webp|gif|bmp)$", "", value, flags=re.I)
    value = re.sub(r"\b(?:test[ -]?point|edl|brom|bootrom|direct[ -]?pinout|pinout|photo|image|download)\b", " ", value, flags=re.I)
    value = re.sub(r"(?:-|_)?\d{9,13}$", "", value)
    value = re.sub(r"[^A-Za-z0-9.+()]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-._ ")
    return (value or fallback)[:180]


def unique_output(directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}.webp"
    if not candidate.exists():
        return candidate
    i = 2
    while (directory / f"{stem}-{i}.webp").exists():
        i += 1
    return directory / f"{stem}-{i}.webp"


def fetch(url: str, *, referer: str | None = None) -> requests.Response:
    headers = {"Referer": referer} if referer else None
    response = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True, headers=headers)
    response.raise_for_status()
    return response


def image_bytes(url: str, *, referer: str | None = None) -> bytes:
    response = fetch(url, referer=referer)
    data = response.content
    ctype = response.headers.get("content-type", "").lower()
    if "text/html" in ctype or data[:32].lstrip().lower().startswith(b"<!doctype html"):
        raise ValueError("URL returned HTML instead of an image")
    if len(data) < 1500:
        raise ValueError("image payload too small")
    return data


def save_as_webp(data: bytes, directory: Path, name: str) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    digest_file = directory / ".digests"
    known = set(digest_file.read_text("utf-8").splitlines()) if digest_file.exists() else set()
    if digest in known:
        raise FileExistsError("duplicate image")
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.width < 220 or im.height < 140:
            raise ValueError(f"image too small: {im.width}x{im.height}")
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        out = unique_output(directory, clean_name(name))
        im.save(out, "WEBP", quality=84, method=6)
    with digest_file.open("a", encoding="utf-8") as f:
        f.write(digest + "\n")
    return out


def collect_parallel(items: list[tuple[str, str, Path, str | None]], workers: int = 8) -> tuple[int, int, list[str]]:
    saved = failed = 0
    notes: list[str] = []

    def one(item: tuple[str, str, Path, str | None]) -> str:
        url, name, directory, referer = item
        data = image_bytes(url, referer=referer)
        return str(save_as_webp(data, directory, name).relative_to(ROOT))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, item): item for item in items}
        for future in as_completed(futures):
            url, name, _, _ = futures[future]
            try:
                print(f"SAVED {future.result()}", flush=True)
                saved += 1
            except FileExistsError:
                pass
            except Exception as exc:
                failed += 1
                if len(notes) < 100:
                    notes.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"FAILED {url}: {exc}", flush=True)
    return saved, failed, notes


def collect_sigma() -> None:
    source = "sigmakey-huawei"
    page = "https://sigmakey.com/en/sigma-help/testpoints-pinouts/?brand=4"
    try:
        soup = BeautifulSoup(fetch(page).text, "html.parser")
        items = []
        seen = set()
        for tag in soup.select("a[href], img[src], img[data-src]"):
            raw = tag.get("href") or tag.get("data-src") or tag.get("src")
            if not raw:
                continue
            url = urljoin(page, raw)
            low = url.lower()
            if "/content/nfs/testpoints/" not in low or not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", low) or url in seen:
                continue
            seen.add(url)
            filename = Path(urlparse(url).path).stem
            context = " ".join(tag.parent.stripped_strings) if tag.parent else ""
            if "testpoint" not in f"{filename} {context}".lower() and "test point" not in f"{filename} {context}".lower():
                continue
            name = re.sub(r"^TESTPOINT[ _-]*HUAWEI[ _-]*", "", filename, flags=re.I)
            name = re.sub(r"^HUAWEI[ _-]*", "", name, flags=re.I)
            items.append((url, name, ROOT / "test-points" / "huawei", page))
        if not items:
            raise RuntimeError("no direct Huawei test-point image URLs found")
        saved, failed, notes = collect_parallel(items, workers=8)
        update_report(source, "success" if saved else "failed", saved, failed, notes + [f"discovered={len(items)}"])
    except Exception as exc:
        update_report(source, "failed", 0, 1, [f"{type(exc).__name__}: {exc}"])
        raise


def collect_easyjtag() -> None:
    source = "easy-jtag"
    page = "https://easy-jtag.com/phones-pinouts/"
    try:
        soup = BeautifulSoup(fetch(page).text, "html.parser")
        items = []
        seen = set()
        for tag in soup.select("a[href], img[src], img[data-src]"):
            raw = tag.get("href") or tag.get("data-src") or tag.get("src")
            if not raw:
                continue
            url = urljoin(page, raw)
            low = url.lower()
            if "/wp-content/uploads/" not in low or not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", low) or "pinout" not in low or url in seen:
                continue
            seen.add(url)
            stem = Path(urlparse(url).path).stem
            if "_ufs_" in low or "-ufs-" in low:
                folder = ROOT / "isp-pinouts" / "ufs"
                name = re.sub(r"[_-]ufs[_-].*$", "", stem, flags=re.I)
            elif "_emmc_" in low or "-emmc-" in low:
                folder = ROOT / "isp-pinouts" / "emmc"
                name = re.sub(r"[_-]emmc[_-].*$", "", stem, flags=re.I)
            else:
                folder = ROOT / "isp-pinouts" / "unclassified"
                name = stem
            items.append((url, name, folder, page))
        if not items:
            raise RuntimeError("no EasyJTAG pinout images found")
        saved, failed, notes = collect_parallel(items, workers=10)
        update_report(source, "success" if saved else "failed", saved, failed, notes + [f"discovered={len(items)}"])
    except Exception as exc:
        update_report(source, "failed", 0, 1, [f"{type(exc).__name__}: {exc}"])
        raise


def collect_passware() -> None:
    source = "passware-unisoc"
    page = "https://support.passware.com/hc/en-us/articles/22372229519383-Unisoc-test-point-gallery"
    try:
        soup = BeautifulSoup(fetch(page).text, "html.parser")
        items = []
        for a in soup.select("a[href*='/hc/article_attachments/']"):
            items.append((urljoin(page, a.get("href")), a.get_text(" ", strip=True) or a.get("title") or "unisoc-device", ROOT / "test-points" / "unisoc", page))
        if not items:
            raise RuntimeError("no Passware attachment links found")
        saved, failed, notes = collect_parallel(items, workers=6)
        update_report(source, "success" if saved else "failed", saved, failed, notes + [f"discovered={len(items)}"])
    except Exception as exc:
        update_report(source, "failed", 0, 1, [f"{type(exc).__name__}: {exc}"])
        raise


def sitemap_urls(base: str) -> list[str]:
    candidates = [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml"), urljoin(base, "/post-sitemap.xml")]
    visited = set()
    output = set()

    def parse(url: str, depth: int = 0) -> None:
        if url in visited or depth > 2:
            return
        visited.add(url)
        try:
            root = ET.fromstring(fetch(url).content)
        except Exception:
            return
        for elem in root.iter():
            if not elem.tag.lower().endswith("loc") or not elem.text:
                continue
            loc = elem.text.strip()
            if loc.endswith(".xml"):
                parse(loc, depth + 1)
            else:
                output.add(loc)

    for candidate in candidates:
        parse(candidate)
    return sorted(output)


def collect_unlocktool() -> None:
    source = "unlocktool-edl"
    base = "https://edl.unlocktool.net/"
    try:
        urls = sitemap_urls(base)
        post_urls = [u for u in urls if urlparse(u).netloc == urlparse(base).netloc and not re.search(r"/(?:tag|category|page)/", u)]
        if not post_urls:
            soup = BeautifulSoup(fetch(base).text, "html.parser")
            post_urls = [urljoin(base, a.get("href")) for a in soup.select("h2 a[href], h3 a[href], article a[href]")]
        post_urls = list(dict.fromkeys(post_urls))[:700]
        if not post_urls:
            raise RuntimeError("no UnlockTool post URLs found")
        saved = failed = 0
        notes = []

        def process_post(url: str) -> str:
            soup = BeautifulSoup(fetch(url).text, "html.parser")
            title_tag = soup.select_one("h1, meta[property='og:title'], title")
            title = title_tag.get("content") if title_tag and title_tag.name == "meta" else (title_tag.get_text(" ", strip=True) if title_tag else Path(urlparse(url).path).stem)
            candidates = []
            og = soup.select_one("meta[property='og:image']")
            if og and og.get("content"):
                candidates.append(urljoin(url, og.get("content")))
            for img in soup.select("article img, .post img, .entry-content img, main img"):
                raw = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
                if raw:
                    candidates.append(urljoin(url, raw))
            candidates = [u for u in dict.fromkeys(candidates) if not re.search(r"(?:logo|avatar|icon|emoji|favicon)", u, re.I)]
            best = None
            for image_url in candidates[:12]:
                try:
                    data = image_bytes(image_url, referer=url)
                    with Image.open(io.BytesIO(data)) as im:
                        score = im.width * im.height
                        if im.width >= 320 and im.height >= 180 and (best is None or score > best[0]):
                            best = (score, data)
                except Exception:
                    continue
            if best is None:
                raise RuntimeError("no usable post image")
            is_isp = bool(re.search(r"\b(?:isp|ips|pinout)\b", title, re.I)) and not bool(re.search(r"\b(?:edl|test[ -]?point)\b", title, re.I))
            directory = ROOT / ("isp-pinouts" if is_isp else "test-points") / "unlocktool"
            return str(save_as_webp(best[1], directory, title).relative_to(ROOT))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(process_post, u): u for u in post_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    print(f"SAVED {future.result()}", flush=True)
                    saved += 1
                except FileExistsError:
                    pass
                except Exception as exc:
                    failed += 1
                    if len(notes) < 100:
                        notes.append(f"{url}: {type(exc).__name__}: {exc}")
        update_report(source, "success" if saved else "failed", saved, failed, notes + [f"posts={len(post_urls)}"])
    except Exception as exc:
        update_report(source, "failed", 0, 1, [f"{type(exc).__name__}: {exc}"])
        raise


def mediafire_direct(page_url: str) -> str:
    soup = BeautifulSoup(fetch(page_url).text, "html.parser")
    button = soup.select_one("a#downloadButton[href], a.input.popsok[href], a[aria-label*='Download'][href]")
    if not button:
        raise RuntimeError("MediaFire direct-download button not found")
    return urljoin(page_url, button.get("href"))


def collect_sumatech() -> None:
    source = "sumatech"
    page = "https://www.sumatechsolution.com/p/test-point.html"
    try:
        soup = BeautifulSoup(fetch(page).text, "html.parser")
        links = [(a.get_text(" ", strip=True), a.get("href")) for a in soup.select("a[href*='mediafire.com']")]
        if not links:
            raise RuntimeError("no MediaFire links found")
        saved = failed = 0
        notes = []
        sevenzip = shutil.which("7z") or shutil.which("7zz")
        for index, (label, link) in enumerate(links, 1):
            try:
                direct = mediafire_direct(link)
                response = fetch(direct, referer=link)
                data = response.content
                ctype = response.headers.get("content-type", "").lower()
                isp = "isp" in label.lower() or "pinout" in label.lower()
                directory = ROOT / ("isp-pinouts" if isp else "test-points") / "sumatech"
                if "image/" in ctype:
                    save_as_webp(data, directory, label)
                    saved += 1
                else:
                    if not sevenzip:
                        raise RuntimeError("7z is unavailable for archive extraction")
                    with tempfile.TemporaryDirectory() as tmp:
                        archive = Path(tmp) / "download.bin"
                        archive.write_bytes(data)
                        extract_dir = Path(tmp) / "extract"
                        extract_dir.mkdir()
                        proc = subprocess.run([sevenzip, "x", "-y", f"-o{extract_dir}", str(archive)], capture_output=True, text=True, timeout=120)
                        if proc.returncode != 0:
                            raise RuntimeError("archive extraction failed")
                        images = [p for p in extract_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}]
                        if not images:
                            raise RuntimeError("archive contains no image files")
                        for n, image in enumerate(images, 1):
                            save_as_webp(image.read_bytes(), directory, label if len(images) == 1 else f"{label}-{n}")
                            saved += 1
                print(f"SAVED {index}/{len(links)} {label}", flush=True)
            except FileExistsError:
                pass
            except Exception as exc:
                failed += 1
                if len(notes) < 100:
                    notes.append(f"{label}: {type(exc).__name__}: {exc}")
                print(f"FAILED {label}: {exc}", flush=True)
            time.sleep(0.15)
        update_report(source, "success" if saved else "failed", saved, failed, notes + [f"links={len(links)}"])
    except Exception as exc:
        update_report(source, "failed", 0, 1, [f"{type(exc).__name__}: {exc}"])
        raise


def collect_droidwin() -> None:
    source = "droidwin"
    page = "https://droidwin.com/how-to-access-edl-test-point-on-various-android-devices/"
    try:
        response = fetch(page)
        if "request is being verified" in response.text.lower() or "just a moment" in response.text.lower():
            raise RuntimeError("site returned browser-verification page")
        soup = BeautifulSoup(response.text, "html.parser")
        article = soup.select_one("article, .entry-content, main") or soup
        current = "android-device"
        items = []
        for node in article.find_all(["h2", "h3", "h4", "img"]):
            if node.name in {"h2", "h3", "h4"}:
                current = node.get_text(" ", strip=True) or current
                continue
            raw = node.get("data-lazy-src") or node.get("data-src") or node.get("src")
            if not raw:
                continue
            url = urljoin(page, raw)
            alt = node.get("alt") or current
            if re.search(r"(?:logo|avatar|icon|banner|ads?)", f"{url} {alt}", re.I):
                continue
            if re.search(r"(?:test[ -]?point|edl|9008|device)", f"{alt} {current}", re.I):
                items.append((url, alt or current, ROOT / "test-points" / "droidwin", page))
        items = list(dict.fromkeys(items))
        if not items:
            raise RuntimeError("no usable article test-point images found")
        saved, failed, notes = collect_parallel(items, workers=5)
        update_report(source, "success" if saved else "failed", saved, failed, notes + [f"discovered={len(items)}"])
    except Exception as exc:
        update_report(source, "failed", 0, 1, [f"{type(exc).__name__}: {exc}"])
        raise


COLLECTORS = {"sigma": collect_sigma, "easyjtag": collect_easyjtag, "passware": collect_passware, "unlocktool": collect_unlocktool, "sumatech": collect_sumatech, "droidwin": collect_droidwin}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COLLECTORS:
        print(f"usage: {Path(sys.argv[0]).name} <{'|'.join(COLLECTORS)}>", file=sys.stderr)
        return 2
    try:
        COLLECTORS[sys.argv[1]]()
        return 0
    except Exception as exc:
        print(f"SOURCE FAILED {sys.argv[1]}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
