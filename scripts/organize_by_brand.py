from __future__ import annotations

import argparse, hashlib, json, math, os, re, shutil, subprocess, tempfile, unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
ROOT = Path(__file__).resolve().parents[1]
ROOTS = (ROOT / "test-points", ROOT / "isp-pinouts")
SOURCE = {"unlocktool", "unlocktool-edl", "easy-jtag", "easyjtag", "sumatech", "droidwin", "sigmakey", "sigmakey-huawei", "passware", "passware-unisoc", "oracle"}
PROTOCOL = {"emmc", "ufs", "nand", "nvme", "unclassified"}
GENERIC = {"test", "point", "testpoint", "edl", "brom", "bootrom", "tp", "pinout", "pin", "out", "isp", "ips", "emmc", "ufs", "photo", "image", "diagram", "download", "solution", "repair", "device", "mobile", "phone", "official", "full", "guide", "mode", "port", "new", "old", "front", "back", "top", "bottom", "version", "revision", "rev", "v1", "v2", "qualcomm", "mtk", "mediatek", "unisoc", "spreadtrum", "kirin", "exynos", "snapdragon"}
BRANDS = [
    ("honor", (r"\bhonor\b",)), ("realme", (r"\brealme\b", r"\brmx[-_ ]?\d")),
    ("oneplus", (r"\bone[ -]?plus\b",)), ("xiaomi", (r"\bxiaomi\b", r"\bredmi\b", r"\bpoco\b", r"\bmi[ -](?:mix|max|note|pad|a\d|\d)")),
    ("samsung", (r"\bsamsung\b", r"\bsm[-_ ]?[a-z0-9]")), ("huawei", (r"\bhuawei\b", r"\bnzone\b", r"\bmate[ -]?\d", r"\bnova[ -]?\d", r"\bmediapad\b")),
    ("oppo", (r"\boppo\b", r"\bcph[-_ ]?\d")), ("vivo", (r"\bvivo\b", r"\biqoo\b", r"\bpd\d{4}")),
    ("motorola", (r"\bmotorola\b", r"\bmoto\b", r"\bxt\d{4}")), ("nokia", (r"\bnokia\b", r"\bta[-_ ]?\d{3,5}\b")),
    ("lenovo", (r"\blenovo\b", r"\blevono\b")), ("asus", (r"\basus\b", r"\bzenfone\b", r"\bzen[ -]?fone\b")),
    ("lg", (r"\blg\b", r"\blm[-_ ]?[a-z0-9]")), ("zte", (r"\bzte\b", r"\bnubia\b")),
    ("meizu", (r"\bmeizu\b",)), ("itel", (r"\bitel\b",)), ("infinix", (r"\binfinix\b",)),
    ("tecno", (r"\btecno\b",)), ("vsmart", (r"\bvsmart\b",)), ("alcatel", (r"\balcatel\b", r"\btcl\b")),
    ("sony", (r"\bsony\b", r"\bxperia\b")), ("google", (r"\bgoogle\b", r"\bpixel\b")),
    ("htc", (r"\bhtc\b",)), ("coolpad", (r"\bcoolpad\b",)), ("micromax", (r"\bmicromax\b",)),
    ("lava", (r"\blava\b",)), ("gionee", (r"\bgionee\b",)), ("blackview", (r"\bblackview\b",)),
    ("oukitel", (r"\boukitel\b",)), ("ulefone", (r"\bulefone\b",)), ("doogee", (r"\bdoogee\b",)),
    ("wiko", (r"\bwiko\b",)), ("nothing", (r"\bnothing\b",)), ("sharp", (r"\bsharp\b",)),
    ("leeco", (r"\bleeco\b", r"\bletv\b")), ("karbonn", (r"\bkarbonn\b",)),
    ("qmobile", (r"\bqmobile\b",)), ("jio", (r"\bjio\b",)), ("panasonic", (r"\bpanasonic\b",)),
    ("hisense", (r"\bhisense\b",)), ("haier", (r"\bhaier\b",)), ("unihertz", (r"\bunihertz\b",)),
    ("umidigi", (r"\bumidigi\b",)), ("archos", (r"\barchos\b",)), ("amazon", (r"\bamazon\b", r"\bfire[ -]?tablet\b")),
]
DISPLAY = {name: name.title() for name, _ in BRANDS} | {"lg": "LG", "zte": "ZTE", "htc": "HTC", "qmobile": "QMobile"}

CATALOG_PATH = ROOT / "catalog.json"
REPORT_PATH = ROOT / "organization-report.json"
try:
    _old_catalog = json.loads(CATALOG_PATH.read_text("utf-8"))
    PREVIOUS = {r["path"]: r for r in _old_catalog.get("records", [])}
except Exception:
    PREVIOUS = {}
try:
    PREVIOUS_DUPLICATES = json.loads(REPORT_PATH.read_text("utf-8")).get("duplicate_groups", [])
except Exception:
    PREVIOUS_DUPLICATES = []

@dataclass
class Asset:
    path: Path; kind: str; protocol: str | None; source: str; brand: str; title: str; key: str
    sha: str; width: int; height: int; size: int; phash: int; dhash: int; pixels: np.ndarray = field(repr=False)
    origins: list[str] = field(default_factory=list); aliases: list[str] = field(default_factory=list)
    @property
    def quality(self): return (self.width * self.height, self.size, len(self.title))

def asc(s): return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
def slug(s, lower=False):
    s = re.sub(r"[^A-Za-z0-9.+()]+", "-", asc(s)); s = re.sub(r"-+", "-", s).strip("-._ ")
    return s.lower() if lower else s

def brand_from(text):
    text = asc(text).lower().replace("_", " ")
    for brand, pats in BRANDS:
        if any(re.search(p, text, re.I) for p in pats): return brand
    return None

def fallback_brand(text):
    aliases = {"ticwatch": "mobvoi", "iphone": "apple", "ipad": "apple", "ipod": "apple", "surface": "microsoft", "lumia": "microsoft", "kindle": "amazon", "blackberry": "blackberry"}
    skip = GENERIC | {"other", "file", "unknown", "model", "various", "android", "devices", "device", "multi", "brand", "pinouts", "points", "images", "archive"}
    for token in re.split(r"[^A-Za-z0-9]+", asc(text)):
        low = token.lower()
        if not low or low in skip or not re.search(r"[a-z]", low): continue
        return aliases.get(low, slug(low, lower=True))
    return "other"

def ocr_brand(path):
    if not shutil.which("tesseract"): return None
    try:
        with Image.open(path) as im, tempfile.TemporaryDirectory() as td:
            im.thumbnail((1800, 1800)); p = Path(td) / "i.png"; im.convert("RGB").save(p)
            out = subprocess.run(["tesseract", str(p), "stdout", "--psm", "11"], capture_output=True, text=True, timeout=20)
        return brand_from(out.stdout) if out.returncode == 0 else None
    except Exception: return None

def detect(path):
    rel = path.relative_to(ROOT); parts = [p for p in rel.parts if p.lower() not in SOURCE | PROTOCOL]
    context = " ".join(parts)
    brand = brand_from(context) or ocr_brand(path) or fallback_brand(context)
    source = next((p.lower() for p in rel.parts if p.lower() in SOURCE), "legacy")
    protocol = next((p.lower() for p in rel.parts if p.lower() in PROTOCOL), None)
    if rel.parts[0] == "isp-pinouts" and protocol is None:
        protocol = "ufs" if re.search(r"\bufs\b", path.stem, re.I) else "emmc" if re.search(r"\bemmc\b", path.stem, re.I) else "unclassified"
    return brand, source, protocol

def title_key(stem, brand, kind):
    text = re.sub(r"\b(?:unlocktool(?:-edl)?|easy[- ]?jtag|sumatech|droidwin|sigmakey|passware|oracle)\b", " ", asc(stem), flags=re.I)
    bw = set(re.split(r"[^a-z0-9]+", brand.lower())); kept = []
    for token in re.split(r"[^A-Za-z0-9.+]+", text):
        if token and token.lower() not in GENERIC | bw and token.lower() not in {"lenovo" if brand == "lenovo" else "", "levono" if brand == "lenovo" else ""} and not (brand == "samsung" and token.lower() == "sm"): kept.append(token)
    kept = kept or ["unknown-model"]
    model = slug("-".join(kept))[:150] or "unknown-model"
    title = f"{DISPLAY.get(brand, brand.title())}-{model}-{'ISP' if kind == 'isp-pinouts' else 'Test-Point'}"
    key = re.sub(r"\b(?:TEST|POINT|TESTPOINT|EDL|BROM|BOOTROM|TP|PINOUT|ISP|IPS|EMMC|UFS)\b", " ", asc(title).upper())
    key = key.replace("SM-", ""); key = re.sub(rf"\b{re.escape(DISPLAY.get(brand, brand).upper())}\b", " ", key)
    return title, (re.sub(r"[^A-Z0-9]+", "", key)[:180] or "UNKNOWNMODEL")

def load(path):
    rel = path.relative_to(ROOT); brand, source, protocol = detect(path); title, key = title_key(path.stem, brand, rel.parts[0])
    data = path.read_bytes(); digest = hashlib.sha256(data).hexdigest()
    with Image.open(path) as im:
        im.load(); w, h = im.size; rgb = im.convert("RGB")
        gray = np.asarray(rgb.convert("L").resize((16, 16), Image.Resampling.LANCZOS), dtype=np.int16)
        ph = int("".join("1" if x > gray.mean() else "0" for x in gray.ravel()), 2)
        dgray = np.asarray(rgb.convert("L").resize((17, 16), Image.Resampling.LANCZOS), dtype=np.int16)
        dh = int("".join("1" if x else "0" for x in (dgray[:, 1:] > dgray[:, :-1]).ravel()), 2)
        pixels = np.asarray(rgb.resize((256, 256), Image.Resampling.LANCZOS), dtype=np.int16)
    prev = PREVIOUS.get(str(rel), {})
    origins = list(prev.get("original_paths", [str(rel)])); aliases = list(prev.get("aliases", [path.stem]))
    return Asset(path, rel.parts[0], protocol, source, brand, title, key, digest, w, h, len(data), ph, dh, pixels, origins, aliases)

def scan():
    paths = sorted(p for root in ROOTS if root.exists() for p in root.rglob("*.webp")); assets = []
    for i, p in enumerate(paths, 1):
        assets.append(load(p))
        if i % 100 == 0: print(f"Scanned {i}/{len(paths)}", flush=True)
    return assets

def merge(a, b):
    a.origins = sorted(set(a.origins + b.origins)); a.aliases = sorted(set(a.aliases + b.aliases), key=str.lower)

def exact_dedupe(assets):
    groups = defaultdict(list); removed = []
    for a in assets: groups[a.sha].append(a)
    kept = []
    for sha, group in groups.items():
        group.sort(key=lambda a: a.quality, reverse=True); primary = group[0]; kept.append(primary)
        for dup in group[1:]: merge(primary, dup); removed.append({"kept": str(primary.path.relative_to(ROOT)), "removed": str(dup.path.relative_to(ROOT)), "reason": "exact-sha256", "sha256": sha})
    return kept, removed

def same_pixels(a, b):
    if (a.kind, a.brand, a.key) != (b.kind, b.brand, b.key): return False
    if abs(math.log((a.width / a.height) / (b.width / b.height))) > .025 or (a.phash ^ b.phash).bit_count() > 2 or (a.dhash ^ b.dhash).bit_count() > 2: return False
    diff = np.abs(a.pixels - b.pixels); changed = np.max(diff, axis=2) > 8
    return float(changed.mean()) <= .00025 and float(diff.mean()) <= .35

def visual_dedupe(assets):
    groups = defaultdict(list); removed = []; kept = []
    for a in assets: groups[(a.kind, a.brand, a.key)].append(a)
    for group in groups.values():
        group.sort(key=lambda a: a.quality, reverse=True); unique = []
        for a in group:
            match = next((b for b in unique if same_pixels(a, b)), None)
            if match is None: unique.append(a)
            else: merge(match, a); removed.append({"kept": str(match.path.relative_to(ROOT)), "removed": str(a.path.relative_to(ROOT)), "reason": "pixel-equivalent"})
        kept += unique
    return kept, removed

def destinations(assets):
    groups = defaultdict(list); result = {}
    for a in assets: groups[(a.kind, a.brand, a.key, a.protocol)].append(a)
    for group in groups.values():
        group.sort(key=lambda a: a.quality, reverse=True)
        for i, a in enumerate(group, 1):
            name = slug(a.title) + (("-tp-" if a.kind == "test-points" else "-isp-") + str(i) if i > 1 else "") + ".webp"
            dest = ROOT / a.kind / a.brand / name if a.kind == "test-points" else ROOT / a.kind / a.brand / (a.protocol or "unclassified") / name
            if dest in result: dest = dest.with_name(f"{dest.stem}-{a.sha[:8]}.webp")
            result[dest] = a
    return result

def install(assigned):
    stage = ROOT / ".organize-staging"; shutil.rmtree(stage, ignore_errors=True); stage.mkdir()
    staged = []
    for dest, a in assigned.items():
        tmp = stage / dest.relative_to(ROOT); tmp.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(a.path, tmp); staged.append((tmp, dest))
    for root in ROOTS:
        if root.exists():
            for p in sorted(root.rglob("*.webp"), reverse=True): p.unlink()
            for p in sorted(root.rglob("*"), reverse=True):
                if p.is_dir():
                    try: p.rmdir()
                    except OSError: pass
    for tmp, dest in staged: dest.parent.mkdir(parents=True, exist_ok=True); os.replace(tmp, dest)
    shutil.rmtree(stage, ignore_errors=True)

def write(assigned, original, removed):
    records = []; brands = Counter(); kinds = Counter(); variants = Counter()
    for dest, a in sorted(assigned.items(), key=lambda x: str(x[0]).lower()):
        rel = str(dest.relative_to(ROOT)); brands[a.brand] += 1; kinds[a.kind] += 1; variants[(a.kind, a.brand, a.key)] += 1
        records.append({"path": rel, "kind": a.kind, "protocol": a.protocol, "brand": a.brand, "model_key": a.key, "title": dest.stem, "sha256": a.sha, "width": a.width, "height": a.height, "sources": sorted({next((p.lower() for p in Path(o).parts if p.lower() in SOURCE), "legacy") for o in a.origins}), "original_paths": a.origins, "aliases": a.aliases})
    summary = {"original_images": original, "unique_images": len(records), "duplicates_removed": len(removed), "variant_model_groups_preserved": sum(v > 1 for v in variants.values()), "brands": dict(sorted(brands.items())), "kinds": dict(sorted(kinds.items())), "other_brand_images": brands.get("other", 0)}
    catalog = {"schema_version": 2, "generated_at": datetime.now(timezone.utc).isoformat(), "layout": {"test_points": "test-points/<brand>/<model>-Test-Point[-tp-N].webp", "isp_pinouts": "isp-pinouts/<brand>/<protocol>/<model>-ISP[-isp-N].webp"}, "summary": summary, "records": records}
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2) + "\n")
    REPORT_PATH.write_text(json.dumps({"status": "success", **summary, "duplicate_groups": removed, "rules": ["Source folders removed; provenance retained in catalog.json.", "Exact SHA-256 duplicates removed globally.", "Pixel-equivalent removal is deliberately strict within one brand/model group.", "Different diagrams for the same model are preserved as numbered TP/ISP variants."]}, indent=2) + "\n")
    (ROOT / "README.md").write_text("# testpoints-verse\n\nDevice test-point and ISP pinout images organized by **brand**, not collection source.\n\n- `test-points/<brand>/...`\n- `isp-pinouts/<brand>/<protocol>/...`\n- `catalog.json` preserves source paths, aliases, hashes, dimensions, and provenance.\n- `organization-report.json` records deduplication totals.\n\nDuplicate images are stored once. Genuine alternative diagrams for one model are retained as `-tp-2`, `-tp-3`, `-isp-2`, and similar variants.\n")
    return catalog

def verify(expected=None):
    cat = json.loads(CATALOG_PATH.read_text()); records = cat["records"]; paths = set(); hashes = set(); origins = set(); errors = []
    for r in records:
        p = ROOT / r["path"]; parts = Path(r["path"]).parts
        if r["path"] in paths: errors.append("duplicate catalog path: " + r["path"])
        paths.add(r["path"])
        if not p.exists(): errors.append("missing: " + r["path"]); continue
        if any(x.lower() in SOURCE for x in parts): errors.append("source folder remains: " + r["path"])
        if parts[0] == "test-points" and len(parts) < 3: errors.append("not brand-first: " + r["path"])
        if parts[0] == "isp-pinouts" and len(parts) < 4: errors.append("not brand/protocol-first: " + r["path"])
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if sha != r["sha256"]: errors.append("hash mismatch: " + r["path"])
        if sha in hashes: errors.append("exact duplicate survived: " + r["path"])
        hashes.add(sha); origins.update(r["original_paths"])
        try:
            with Image.open(p) as im: im.verify()
        except Exception as e: errors.append(f"invalid {r['path']}: {e}")
    disk = [p for root in ROOTS for p in root.rglob("*.webp")]
    if len(disk) != len(records): errors.append(f"disk/catalog mismatch {len(disk)}/{len(records)}")
    if expected is not None and len(origins) != expected: errors.append(f"coverage mismatch {len(origins)}/{expected}")
    if errors: raise RuntimeError("\n".join(errors[:100]))
    return {"status": "success", "files": len(records), "original_paths_accounted": len(origins), "exact_duplicate_hashes": 0}

def organize():
    original = scan(); coverage = len({o for a in original for o in a.origins})
    kept, removed1 = exact_dedupe(original); kept, removed2 = visual_dedupe(kept); assigned = destinations(kept)
    merged_removed = []
    seen = set()
    for row in PREVIOUS_DUPLICATES + removed1 + removed2:
        marker = (row.get("removed"), row.get("reason"), row.get("sha256"))
        if marker not in seen: seen.add(marker); merged_removed.append(row)
    install(assigned); cat = write(assigned, coverage, merged_removed); proof = verify(coverage); print(json.dumps({"summary": cat["summary"], "verification": proof}, indent=2))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--verify-only", action="store_true"); args = ap.parse_args()
    print(json.dumps(verify(), indent=2)) if args.verify_only else organize(); return 0

if __name__ == "__main__": raise SystemExit(main())
