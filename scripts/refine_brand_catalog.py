from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"
REPORT = ROOT / "organization-report.json"
BASELINE_COMMIT = "a3c8fbcb984f14d8abf96cd5c2dc7a7872dfa4e5"
ROOTS = (ROOT / "test-points", ROOT / "isp-pinouts")

BRANDS = [
    ("honor", (r"\bhonor\b",)),
    ("realme", (r"\brealme\b", r"\brmx[-_ ]?\d")),
    ("oneplus", (r"\bone[ -]?plus\b",)),
    ("xiaomi", (r"\bxiaomi\b", r"\bredmi\b", r"\bpoco\b", r"\bmi[ -](?:mix|max|note|pad|a\d|\d)")),
    ("samsung", (r"\bsamsung\b", r"\bsm[-_ ]?[a-z0-9]")),
    ("huawei", (r"\bhuawei\b", r"\bnzone\b", r"\bmate[ -]?\d", r"\bnova[ -]?\d", r"\bmediapad\b")),
    ("oppo", (r"\boppo\b", r"\bcph[-_ ]?\d")),
    ("vivo", (r"\bvivo\b", r"\biqoo\b", r"\bpd\d{4}")),
    ("motorola", (r"\bmotorola\b", r"\bmoto\b", r"\bxt\d{4}")),
    ("nokia", (r"\bnokia\b", r"\bta[-_ ]?\d{3,5}\b")),
    ("lenovo", (r"\blenovo\b", r"\blevono\b")),
    ("asus", (r"\basus\b", r"\bzenfone\b", r"\bzen[ -]?fone\b", r"\bme172v\b")),
    ("lg", (r"\blg\b", r"\blm[-_ ]?[a-z0-9]")),
    ("zte", (r"\bzte\b", r"\bnubia\b")),
    ("meizu", (r"\bmeizu\b",)),
    ("itel", (r"\bitel\b",)),
    ("infinix", (r"\binfinix\b",)),
    ("tecno", (r"\btecno\b",)),
    ("vsmart", (r"\bvsmart\b",)),
    ("alcatel", (r"\balcatel\b", r"\btcl\b")),
    ("sony", (r"\bsony\b", r"\bxperia\b")),
    ("google", (r"\bgoogle\b", r"\bpixel\b")),
    ("htc", (r"\bhtc\b",)),
    ("coolpad", (r"\bcoolpad\b",)),
    ("micromax", (r"\bmicromax\b",)),
    ("lava", (r"\blava\b",)),
    ("gionee", (r"\bgionee\b",)),
    ("blackview", (r"\bblackview\b",)),
    ("oukitel", (r"\boukitel\b",)),
    ("ulefone", (r"\bulefone\b",)),
    ("doogee", (r"\bdoogee\b",)),
    ("wiko", (r"\bwiko\b",)),
    ("nothing", (r"\bnothing\b",)),
    ("sharp", (r"\bsharp\b",)),
    ("leeco", (r"\bleeco\b", r"\bletv\b")),
    ("karbonn", (r"\bkarbonn\b",)),
    ("qmobile", (r"\bqmobile\b",)),
    ("jio", (r"\bjio\b",)),
    ("panasonic", (r"\bpanasonic\b",)),
    ("hisense", (r"\bhisense\b",)),
    ("haier", (r"\bhaier\b",)),
    ("unihertz", (r"\bunihertz\b",)),
    ("umidigi", (r"\bumidigi\b",)),
    ("archos", (r"\barchos\b",)),
    ("amazon", (r"\bamazon\b", r"\bkindle\b", r"\bfire[ -]?tablet\b")),
    ("apple", (r"\bapple\b", r"\biphone\b", r"\bipad\b", r"\bipod\b")),
    ("microsoft", (r"\bmicrosoft\b", r"\bsurface\b", r"\blumia\b")),
    ("mobvoi", (r"\bmobvoi\b", r"\bticwatch\b")),
    ("blackberry", (r"\bblackberry\b",)),
    ("4good", (r"\b4good\b",)),
    ("acer", (r"\bacer\b", r"\biconia\b")),
    ("advan", (r"\badvan\b",)),
    ("agm", (r"\bagm\b",)),
    ("amg", (r"\bamg\b",)),
    ("andromax", (r"\bandromax\b", r"\bsmartfren\b")),
    ("artel", (r"\bartel\b",)),
    ("assistant", (r"\bassistant\b",)),
    ("blu", (r"\bblu\b",)),
    ("bq", (r"\bbq\b",)),
    ("bravis", (r"\bbravis\b",)),
    ("bush", (r"\bbush\b",)),
    ("cat", (r"\bcat[-_ ]?[a-z0-9]",)),
    ("condor", (r"\bcondor\b",)),
    ("crius", (r"\bcrius\b",)),
    ("tp-link", (r"\btp[-_ ]?link\b",)),
    ("luna", (r"\bluna\b",)),
    ("megafon", (r"\bmegafon\b",)),
    ("sigma", (r"\bsigma\b",)),
    ("sky", (r"\bsky[-_ ]?[a-z0-9]", r"\bpantech\b")),
    ("starway", (r"\bstarway\b",)),
    ("tesla", (r"\btesla\b",)),
    ("texet", (r"\btexet\b",)),
    ("tomtom", (r"\btomtom\b",)),
    ("toshiba", (r"\btoshiba\b",)),
    ("veon", (r"\bveon\b",)),
    ("yu", (r"\byureka\b", r"\byu[-_ ]?[a-z0-9]")),
    ("zopo", (r"\bzopo\b",)),
]
DISPLAY = {name: name.title() for name, _ in BRANDS}
DISPLAY.update({
    "lg": "LG", "zte": "ZTE", "htc": "HTC", "qmobile": "QMobile",
    "tp-link": "TP-Link", "bq": "BQ", "blu": "BLU", "agm": "AGM",
    "amg": "AMG", "yu": "YU", "4good": "4GOOD",
})
SOURCE_NAMES = {
    "unlocktool", "unlocktool-edl", "easy-jtag", "easyjtag", "sumatech",
    "droidwin", "sigmakey", "sigmakey-huawei", "passware",
    "passware-unisoc", "oracle", "legacy",
}
NOISE = {
    "test", "point", "testpoint", "edl", "brom", "bootrom", "tp",
    "pinout", "pin", "out", "isp", "ips", "emmc", "ufs", "image",
    "photo", "diagram", "download", "solution", "unlocktool", "easyjtag",
    "sumatech", "droidwin", "sigmakey", "passware", "oracle", "other",
    "source", "official", "file", "webp",
}


def ascii_text(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9.+()]+", "-", ascii_text(value))
    return re.sub(r"-+", "-", value).strip("-._ ") or "unknown-model"


def detect_brand(text: str, existing: str | None = None) -> str:
    normalized = ascii_text(text).lower().replace("_", " ")
    for brand, patterns in BRANDS:
        if any(re.search(pattern, normalized, re.I) for pattern in patterns):
            return brand
    if existing and existing not in {"other", "webp", "link", "me172v"}:
        return existing
    return "other"


def baseline_catalog() -> dict:
    try:
        result = subprocess.run(
            ["git", "show", f"{BASELINE_COMMIT}:catalog.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError(f"cannot recover baseline catalog from {BASELINE_COMMIT}: {exc}")


def provenance_index(baseline: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for record in baseline.get("records", []):
        for origin in record.get("original_paths", []):
            index[origin] = record
    return index


def baseline_record(current: dict, index: dict[str, dict]) -> dict | None:
    matches = [index[o] for o in current.get("original_paths", []) if o in index]
    if not matches:
        return None
    matches.sort(key=lambda r: len(r.get("original_paths", [])), reverse=True)
    return matches[0]


def model_label(record: dict, base: dict | None, brand: str) -> str:
    candidates: list[str] = []
    if base:
        candidates.extend(base.get("aliases", []))
        candidates.append(base.get("title", ""))
    candidates.extend(record.get("aliases", []))
    candidates.append(record.get("title", ""))

    brand_words = set(re.split(r"[^a-z0-9]+", brand.lower()))
    display_words = set(re.split(r"[^a-z0-9]+", DISPLAY.get(brand, brand).lower()))
    best: tuple[int, str] | None = None

    for raw in candidates:
        text = re.sub(r"-(?:tp|isp)-\d+$", "", ascii_text(raw), flags=re.I)
        tokens: list[str] = []
        for token in re.split(r"[^A-Za-z0-9.+]+", text):
            low = token.lower()
            if not token or low in NOISE or low in brand_words or low in display_words:
                continue
            if brand == "samsung" and low == "sm":
                continue
            tokens.append(token)
        if not tokens:
            continue
        candidate = slug("-".join(tokens))
        score = (len(candidate), candidate.lower())
        if best is None or score < (best[0], best[1].lower()):
            best = (len(candidate), candidate)

    return best[1] if best else "unknown-model"


def source_list(record: dict) -> list[str]:
    sources = set(record.get("sources", []))
    for origin in record.get("original_paths", []):
        for part in Path(origin).parts:
            if part.lower() in SOURCE_NAMES:
                sources.add(part.lower())
    return sorted(sources or {"legacy"})


def copy_stage(assignments: list[tuple[Path, Path]]) -> None:
    stage = ROOT / ".refine-staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    staged: list[tuple[Path, Path]] = []
    for source, destination in assignments:
        temporary = stage / destination.relative_to(ROOT)
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, temporary)
        staged.append((temporary, destination))

    for root in ROOTS:
        for path in sorted(root.rglob("*.webp"), reverse=True):
            path.unlink()
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    for temporary, destination in staged:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
    shutil.rmtree(stage, ignore_errors=True)


def refine() -> dict:
    current = json.loads(CATALOG.read_text("utf-8"))
    baseline = baseline_catalog()
    index = provenance_index(baseline)
    enriched: list[dict] = []

    for record in current.get("records", []):
        source_path = ROOT / record["path"]
        if not source_path.exists():
            raise FileNotFoundError(record["path"])
        base = baseline_record(record, index)
        evidence = " ".join(
            record.get("original_paths", [])
            + record.get("aliases", [])
            + [record.get("title", ""), base.get("title", "") if base else ""]
        )
        brand = detect_brand(evidence, record.get("brand"))
        model_key = (base or record).get("model_key", record.get("model_key", "UNKNOWNMODEL"))
        enriched.append({
            **record,
            "_source_path": source_path,
            "_base": base,
            "brand": brand,
            "model_key": model_key,
            "model_label": model_label(record, base, brand),
            "sources": source_list(record),
        })

    groups: dict[tuple[str, str, str, str | None], list[dict]] = defaultdict(list)
    for record in enriched:
        groups[(record["kind"], record["brand"], record["model_key"], record.get("protocol"))].append(record)

    assignments: list[tuple[Path, Path]] = []
    new_records: list[dict] = []
    variants = 0
    used_paths: set[str] = set()

    for (kind, brand, model_key, protocol), records in sorted(groups.items()):
        records.sort(key=lambda r: (r.get("width", 0) * r.get("height", 0), r.get("sha256", "")), reverse=True)
        if len(records) > 1:
            variants += 1
        base_model = min((r["model_label"] for r in records), key=lambda value: (len(value), value.lower()))
        prefix = f"{DISPLAY.get(brand, brand.title())}-{base_model}"
        suffix = "Test-Point" if kind == "test-points" else "ISP"

        for number, record in enumerate(records, 1):
            variant = "" if number == 1 else f"-{'tp' if kind == 'test-points' else 'isp'}-{number}"
            filename = f"{slug(prefix)}-{suffix}{variant}.webp"
            if kind == "test-points":
                destination = ROOT / kind / brand / filename
            else:
                destination = ROOT / kind / brand / (protocol or "unclassified") / filename
            relative = str(destination.relative_to(ROOT))
            if relative in used_paths:
                destination = destination.with_name(f"{destination.stem}-{record['sha256'][:8]}.webp")
                relative = str(destination.relative_to(ROOT))
            used_paths.add(relative)
            assignments.append((record["_source_path"], destination))
            new_records.append({
                "path": relative,
                "kind": kind,
                "protocol": protocol,
                "brand": brand,
                "model_key": model_key,
                "title": destination.stem,
                "sha256": record["sha256"],
                "width": record["width"],
                "height": record["height"],
                "sources": record["sources"],
                "original_paths": sorted(set(record.get("original_paths", []))),
                "aliases": sorted(set(record.get("aliases", [])), key=str.lower),
            })

    copy_stage(assignments)
    brands = Counter(record["brand"] for record in new_records)
    kinds = Counter(record["kind"] for record in new_records)
    old_report = json.loads(REPORT.read_text("utf-8"))
    summary = {
        "original_images": old_report.get("original_images", 1915),
        "unique_images": len(new_records),
        "duplicates_removed": old_report.get("duplicates_removed", 74),
        "variant_model_groups_preserved": variants,
        "brands": dict(sorted(brands.items())),
        "kinds": dict(sorted(kinds.items())),
        "other_brand_images": brands.get("other", 0),
    }
    output_catalog = {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layout": {
            "test_points": "test-points/<brand>/<model>-Test-Point[-tp-N].webp",
            "isp_pinouts": "isp-pinouts/<brand>/<protocol>/<model>-ISP[-isp-N].webp",
        },
        "summary": summary,
        "records": sorted(new_records, key=lambda r: r["path"].lower()),
    }
    CATALOG.write_text(json.dumps(output_catalog, indent=2) + "\n", "utf-8")
    REPORT.write_text(json.dumps({
        "status": "success",
        **summary,
        "duplicate_groups": old_report.get("duplicate_groups", []),
        "rules": [
            "Folders are brand-first; collection source names are metadata only.",
            "Exact duplicate image bytes are stored once.",
            "Model grouping is recovered from the pre-sort provenance catalog.",
            "Different images for the same model remain numbered TP/ISP variants.",
            "Unknown brands remain in other instead of being guessed from a model token.",
        ],
    }, indent=2) + "\n", "utf-8")
    return output_catalog


def verify() -> dict:
    catalog = json.loads(CATALOG.read_text("utf-8"))
    records = catalog.get("records", [])
    errors: list[str] = []
    paths: set[str] = set()
    hashes: set[str] = set()
    variant_groups: Counter[tuple[str, str, str, str | None]] = Counter()

    for record in records:
        relative = record["path"]
        path = ROOT / relative
        parts = Path(relative).parts
        if relative in paths:
            errors.append(f"duplicate path: {relative}")
        paths.add(relative)
        if not path.exists():
            errors.append(f"missing file: {relative}")
            continue
        if record["kind"] == "test-points" and len(parts) < 3:
            errors.append(f"not brand-first: {relative}")
        if record["kind"] == "isp-pinouts" and len(parts) < 4:
            errors.append(f"not brand/protocol-first: {relative}")
        if parts[1] != record["brand"]:
            errors.append(f"brand mismatch: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record["sha256"]:
            errors.append(f"hash mismatch: {relative}")
        if digest in hashes:
            errors.append(f"exact duplicate survived: {relative}")
        hashes.add(digest)
        variant_groups[(record["kind"], record["brand"], record["model_key"], record.get("protocol"))] += 1
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            errors.append(f"invalid image {relative}: {exc}")

    disk_files = [path for root in ROOTS for path in root.rglob("*.webp")]
    if len(disk_files) != len(records):
        errors.append(f"disk/catalog mismatch: {len(disk_files)}/{len(records)}")
    reported_variants = catalog["summary"]["variant_model_groups_preserved"]
    actual_variants = sum(count > 1 for count in variant_groups.values())
    if reported_variants != actual_variants:
        errors.append(f"variant count mismatch: {reported_variants}/{actual_variants}")
    if errors:
        raise RuntimeError("\n".join(errors[:100]))
    return {
        "status": "success",
        "files": len(records),
        "exact_duplicate_hashes": 0,
        "variant_model_groups_preserved": actual_variants,
        "other_brand_images": catalog["summary"].get("other_brand_images", 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = verify() if args.verify_only else {"summary": refine()["summary"]}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
