from __future__ import annotations

import hashlib
import io
import json
import re
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
ARTICLE_URL = "https://droidwin.com/how-to-access-edl-test-point-on-various-android-devices/"
SLUG = "how-to-access-edl-test-point-on-various-android-devices"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36"
LOCK = threading.Lock()


def session() -> requests.Session:
    value = requests.Session()
    value.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json,image/avif,image/webp,image/apng,*/*;q=0.8",
    })
    return value


def clean_name(value: str) -> str:
    value = unquote(value or "")
    value = re.sub(r"^EDL Test Point for\s+", "", value, flags=re.I)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9.+()]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-._ ")[:180] or "unknown-device"


def report() -> dict:
    if REPORT_PATH.exists():
        try:
            return json.loads(REPORT_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"sources": {}}


def write_report(payload: dict) -> None:
    value = report()
    value.setdefault("sources", {})["droidwin"] = payload
    REPORT_PATH.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", "utf-8")


def add_candidate(items: list[tuple[str, str]], seen: set[str], raw_url: str, title: str, alt: str = "") -> None:
    absolute = urljoin(ARTICLE_URL, raw_url.strip())
    combined = f"{title} {alt} {absolute}"
    if absolute in seen:
        return
    if not re.search(r"(?:test[ -]?point|\bedl\b)", combined, re.I):
        return
    if re.search(r"(?:logo|avatar|icon|banner|advert|driver|cable|signature|enforcement)", combined, re.I):
        return
    if not re.search(r"\.(?:jpe?g|png|webp|bmp)(?:$|\?)", absolute, re.I):
        return
    seen.add(absolute)
    items.append((absolute, title))


def from_wordpress(items: list[tuple[str, str]], seen: set[str], notes: list[str]) -> None:
    endpoints = [
        f"https://droidwin.com/wp-json/wp/v2/posts?slug={SLUG}&per_page=1",
        f"https://droidwin.com/?rest_route=/wp/v2/posts&slug={SLUG}&per_page=1",
    ]
    for endpoint in endpoints:
        try:
            response = session().get(endpoint, timeout=45)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                continue
            rendered = payload[0].get("content", {}).get("rendered", "")
            soup = BeautifulSoup(rendered, "html.parser")
            current = ""
            for node in soup.find_all(["h2", "h3", "h4", "img"]):
                if node.name in {"h2", "h3", "h4"}:
                    heading = node.get_text(" ", strip=True)
                    current = heading if re.search(r"EDL Test Point for", heading, re.I) else ""
                    continue
                if not current:
                    continue
                raw = (
                    node.get("data-lazy-src")
                    or node.get("data-src")
                    or node.get("data-orig-file")
                    or node.get("src")
                )
                if raw:
                    add_candidate(items, seen, raw, current, node.get("alt") or "")
            if items:
                notes.append(f"wordpress-api discovered {len(items)} image references")
                return
        except Exception as exc:
            notes.append(f"wordpress-api {endpoint}: {type(exc).__name__}: {exc}")


def from_reader(items: list[tuple[str, str]], seen: set[str], notes: list[str]) -> None:
    reader_urls = [
        f"https://r.jina.ai/http://droidwin.com/{SLUG}/",
        f"https://r.jina.ai/https://droidwin.com/{SLUG}/",
    ]
    image_pattern = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)")
    for reader_url in reader_urls:
        try:
            response = session().get(reader_url, timeout=90)
            response.raise_for_status()
            current = ""
            before = len(items)
            for line in response.text.splitlines():
                heading = re.match(r"^#{2,4}\s+(.+?)\s*$", line)
                if heading:
                    text = heading.group(1).strip()
                    current = text if re.search(r"EDL Test Point for", text, re.I) else ""
                    continue
                if not current:
                    continue
                for alt, image_url in image_pattern.findall(line):
                    add_candidate(items, seen, image_url, current, alt)
            if len(items) > before:
                notes.append(f"reader discovered {len(items) - before} additional image references")
                return
        except Exception as exc:
            notes.append(f"reader {reader_url}: {type(exc).__name__}: {exc}")


def save_image(image_url: str, title: str) -> str:
    response = session().get(image_url, headers={"Referer": ARTICLE_URL}, timeout=60, allow_redirects=True)
    response.raise_for_status()
    data = response.content
    if len(data) < 1200 or "text/html" in (response.headers.get("content-type") or "").lower():
        raise ValueError("image request did not return a usable image")
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.width < 100 or image.height < 100:
            raise ValueError(f"image too small: {image.width}x{image.height}")
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=84, method=6)
        output = buffer.getvalue()

    directory = ROOT / "test-points" / "droidwin"
    directory.mkdir(parents=True, exist_ok=True)
    stem = clean_name(title)
    digest = hashlib.sha256(output).hexdigest()[:8]
    with LOCK:
        target = directory / f"{stem}.webp"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(output).digest():
                raise FileExistsError("already saved")
            target = directory / f"{stem}-{digest}.webp"
        target.write_bytes(output)
    return str(target.relative_to(ROOT))


def main() -> int:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    notes: list[str] = []
    from_wordpress(items, seen, notes)
    from_reader(items, seen, notes)

    saved = skipped = failed = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(save_image, url, title): (url, title) for url, title in items}
        for future in as_completed(futures):
            url, title = futures[future]
            try:
                print(f"SAVED {future.result()}", flush=True)
                saved += 1
            except FileExistsError:
                skipped += 1
            except Exception as exc:
                failed += 1
                failures.append(f"{title}: {type(exc).__name__}: {exc} [{url}]")

    status = "success" if saved + skipped >= 19 and failed == 0 else "partial" if saved + skipped else "blocked"
    write_report({
        "status": status,
        "discovered": len(items),
        "saved": saved,
        "skipped_existing": skipped,
        "failed": failed,
        "notes": (notes + failures)[:150],
    })
    return 0 if saved + skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
