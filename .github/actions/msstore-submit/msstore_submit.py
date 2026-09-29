#!/usr/bin/env python3
"""Microsoft Store submit: push listing text and release notes, stage
screenshots, upload the .msix, and submit for certification. Gated by a
GitHub Environment's required-reviewer approval at the workflow level, not
in here — this script runs only once that approval has been given.

The submission API is wrapped by Microsoft's own `msstore` CLI (the Store
Developer CLI), which this shells out to for every call that has one:
`apps get`, `submission get/update/publish/delete/status`. The one thing it
does not wrap is the file upload, which is a direct Azure block-blob PUT
against a short-lived SAS URL the draft hands back.

The sequence and the document's shape (the whole submission is carried
untyped and PUT back whole — a partial update silently drops fields; a
picture and a package are matched to the listing by file name alone; a
first-ever submission cannot be made by this API at all) are ported from
excelano/ship's internal/store package, which had already found these the
hard way. This script carries no authority from ship's own design — only
that domain knowledge.

Author: David M. Anderson
Built with AI assistance (Claude, Anthropic)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

CLI = "msstore"
MOST_SCREENSHOTS = 10
BLOCK_SIZE = 8 << 20

LIMITS = {
    "microsoft.short-description": 500,
    "microsoft-review-notes": 4000,
}


def err(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"::warning::{msg}")


def refuse(msg: str) -> None:
    err(msg)
    sys.exit(1)


def key(locale: str) -> str:
    """The language as ship.conf writes it and the Microsoft Store keys it:
    en-us, de-de — lowercase, unlike Apple's en-US/de-DE."""
    return locale.lower()


_LOG_LINE = re.compile(r"^.*?\d\d:\d\d:\d\d\s+\w+:\s+\S+\s+")


def complaint(output: str) -> str:
    """The tool's own words with its logging preamble stripped, so a
    refusal reads as a sentence rather than someone else's log."""
    lines = []
    for line in output.splitlines():
        line = _LOG_LINE.sub("", line.strip()).strip().rstrip(".")
        if line:
            lines.append(line)
    return "; ".join(lines) if lines else output.strip()


def run_cli(*args: str, check_output=True) -> str:
    proc = subprocess.run([CLI, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(complaint(proc.stdout + "\n" + proc.stderr))
    return proc.stdout if check_output else (proc.stdout + proc.stderr)


def cli_ready() -> None:
    if subprocess.run(["which", CLI], capture_output=True).returncode != 0:
        refuse(f"no {CLI} on PATH; install the Microsoft Store Developer CLI first "
               "(microsoft/microsoft-store-apppublisher, or msstore-cli via winget/brew)")
    try:
        run_cli("info")
    except RuntimeError as e:
        refuse(f"{CLI} has no credentials: {e}. Configure with 'msstore reconfigure "
               "--tenantId ... --sellerId ... --clientId ... --clientSecret ...'")


def app_get(product_id: str) -> dict:
    try:
        out = run_cli("apps", "get", product_id)
    except RuntimeError as e:
        refuse(f"reading {product_id}: {e}")
    i = out.find("{")
    if i < 0:
        refuse(f"reading {product_id}: no product came back")
    return json.loads(out[i:])


def app_submitted(app: dict) -> bool:
    return bool(app.get("LastPublishedApplicationSubmission")) or bool(app.get("PendingApplicationSubmission"))


def app_has_draft(app: dict) -> bool:
    return bool(app.get("PendingApplicationSubmission"))


def submission_get(product_id: str) -> dict:
    try:
        out = run_cli("submission", "get", product_id)
    except RuntimeError as e:
        refuse(f"reading the submission for {product_id}: {e}")
    return json.loads(out)


def submission_update(product_id: str, doc: dict) -> None:
    import tempfile, os
    fd, path = tempfile.mkstemp(prefix="msstore-submission-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        proc = subprocess.run([CLI, "submission", "update", product_id, "--payload", path, "-v"],
                               capture_output=True, text=True)
        if proc.returncode != 0:
            said = complaint(proc.stdout + proc.stderr)
            refuse(f"updating the submission: {said[-2000:]}\n"
                   f"      a draft may have been left open, carrying the release before this one; "
                   f"'{CLI} submission delete {product_id}' removes it")
    finally:
        os.remove(path)


def submission_publish(product_id: str) -> None:
    try:
        run_cli("submission", "publish", product_id)
    except RuntimeError as e:
        refuse(f"committing the submission: {e}")


_STATE_LINE = re.compile(r"^.*Submission Status\s*=\s*(.+)$", re.M)


def submission_state(product_id: str) -> str:
    proc = subprocess.run([CLI, "submission", "status", product_id], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    m = _STATE_LINE.search(out)
    if m:
        return m.group(1).strip()
    lines = out.strip().splitlines()
    return lines[-1].strip() if lines else ""


# ---- the submission document (an untyped dict, PUT back whole) -----------

def doc_locales(doc: dict) -> list[str]:
    return sorted((doc.get("Listings") or {}).keys())


def doc_add_locale(doc: dict, locale: str) -> None:
    listings = doc.setdefault("Listings", {})
    if locale in listings:
        return
    template = None
    for v in listings.values():
        if isinstance(v, dict):
            template = v
            break
    if template is None:
        refuse(f"no existing language to clone {locale}'s shape from")
    clone = json.loads(json.dumps(template))
    base = clone.get("BaseListing")
    if isinstance(base, dict):
        base["Images"] = []
    listings[locale] = clone


def doc_set_release_notes(doc: dict, notes: dict[str, str]) -> list[str]:
    missing = []
    for locale, l in (doc.get("Listings") or {}).items():
        base = (l or {}).get("BaseListing")
        if not isinstance(base, dict):
            continue
        if locale not in notes:
            missing.append(locale)
            continue
        base["ReleaseNotes"] = notes[locale]
    return sorted(missing)


def doc_set_listing(doc: dict, copy: dict[str, dict]) -> list[str]:
    missing = []
    for locale, l in (doc.get("Listings") or {}).items():
        base = (l or {}).get("BaseListing")
        if not isinstance(base, dict):
            continue
        one = copy.get(locale)
        if one is None:
            missing.append(locale)
            continue
        if one.get("title"):
            base["Title"] = one["title"]
        if one.get("description"):
            base["Description"] = one["description"]
        if one.get("short"):
            base["ShortDescription"] = one["short"]
        if one.get("features"):
            base["Features"] = one["features"]
        if one.get("keywords"):
            base["Keywords"] = one["keywords"]
    return sorted(missing)


def doc_set_certification_notes(doc: dict, text: str) -> bool:
    if doc.get("NotesForCertification") == text:
        return False
    doc["NotesForCertification"] = text
    return True


def doc_set_images(doc: dict, sets: dict[str, list[str]]) -> list[str]:
    """sets: locale -> file names already staged for upload."""
    over = [f"{loc} has {len(names)}" for loc, names in sets.items() if len(names) > MOST_SCREENSHOTS]
    if over:
        refuse(f"a listing takes {MOST_SCREENSHOTS} screenshots and " + ", ".join(over))
    retired = []
    for locale, l in (doc.get("Listings") or {}).items():
        want = sets.get(locale)
        if want is None:
            continue
        base = (l or {}).get("BaseListing")
        if not isinstance(base, dict):
            continue
        coming = set(want)
        kept = []
        for img in base.get("Images") or []:
            if img.get("ImageType") != "Screenshot":
                kept.append(img)
                continue
            name = img.get("FileName")
            if name in coming:
                continue
            if img.get("FileStatus") != "PendingDelete":
                img["FileStatus"] = "PendingDelete"
                retired.append(f"{locale} {name}")
            kept.append(img)
        for name in want:
            kept.append({"FileName": name, "FileStatus": "PendingUpload", "ImageType": "Screenshot"})
        base["Images"] = kept
    return sorted(retired)


def doc_set_package(doc: dict, file_name: str) -> list[str]:
    retired = []
    kept = []
    for p in doc.get("ApplicationPackages") or []:
        name = p.get("FileName")
        if name == file_name:
            continue
        if p.get("FileStatus") != "PendingDelete":
            p["FileStatus"] = "PendingDelete"
            retired.append(f"{name} {p.get('Version', '')}".strip())
        kept.append(p)
    kept.append({"FileName": file_name, "FileStatus": "PendingUpload"})
    doc["ApplicationPackages"] = kept
    return retired


def doc_carries(doc: dict, file_name: str) -> bool:
    for p in doc.get("ApplicationPackages") or []:
        if p.get("FileStatus") == "PendingDelete":
            continue
        if p.get("FileName") == file_name:
            return True
    return False


def doc_waiting(doc: dict) -> list[str]:
    out = [p["FileName"] for p in doc.get("ApplicationPackages") or [] if p.get("FileStatus") == "PendingUpload"]
    for l in (doc.get("Listings") or {}).values():
        base = (l or {}).get("BaseListing") or {}
        for img in base.get("Images") or []:
            if img.get("FileStatus") == "PendingUpload" and img.get("FileName"):
                out.append(img["FileName"])
    return out


# ---- upload: zip the pending files, PUT as Azure blob blocks -------------

def bundle(dest: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, arcname=f.name)


def push(url: str, path: Path, tick=lambda: None) -> None:
    data = path.read_bytes()
    ids = []
    for i in range(0, max(len(data), 1), BLOCK_SIZE):
        chunk = data[i:i + BLOCK_SIZE]
        block_id = base64.b64encode(f"{len(ids):08d}".encode()).decode()
        _put(f"{url}&comp=block&blockid={block_id}", "application/octet-stream", chunk)
        ids.append(block_id)
        tick()
    if not ids:
        refuse(f"{path.name} is empty; there is nothing to send")
    body = '<?xml version="1.0" encoding="utf-8"?><BlockList>' + \
           "".join(f"<Latest>{i}</Latest>" for i in ids) + "</BlockList>"
    _put(f"{url}&comp=blocklist", "application/xml", body.encode())


def _put(url: str, content_type: str, body: bytes) -> None:
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=300):
            pass
    except urllib.error.HTTPError as e:
        said = e.read().decode(errors="replace")
        m = re.search(r"<Message>(.*?)</Message>", said, re.S)
        refuse(f"{e.code} {e.reason}: {(m.group(1) if m else said).strip().splitlines()[0] if said.strip() else 'no answer'}")


# ---- screenshots on disk ---------------------------------------------------

def read_frames(directory: Path) -> dict[str, list[Path]]:
    """dist/screenshots/<Apple-tag>/<NN-name>.png — Apple's spelling, the
    store the frames were first built for. Converted to Microsoft's lowercase
    key here, at the point of staging, never assumed to already match: a
    prior bug (locale.go's own history) looked up en-US frames in an
    en-us-keyed listing, found nothing, and submitted with no pictures."""
    out: dict[str, list[Path]] = {}
    if not directory.is_dir():
        return out
    for sub in sorted(directory.iterdir()):
        if not sub.is_dir():
            continue
        frames = sorted(p for p in sub.iterdir() if p.suffix.lower() == ".png")
        if frames:
            out[sub.name] = frames
    return out


def check_distinct(sets: dict[str, list[Path]]) -> None:
    seen: dict[str, list[str]] = {}
    for locale in sorted(sets):
        for f in sets[locale]:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            seen.setdefault(digest, []).append(f"{locale}/{f.name}")
    dupes = [" = ".join(names) for names in seen.values() if len(names) > 1]
    if dupes:
        refuse("the same picture more than once: " + "; ".join(dupes))


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def product_root(doc: dict, product_key: str) -> dict:
    if not product_key:
        return doc
    try:
        return doc["products"][product_key]
    except KeyError:
        refuse(f"no [products.{product_key}] table")


def check_limit(name: str, text: str) -> None:
    limit = LIMITS.get(name)
    if limit and len(text) > limit:
        refuse(f"{name} is {len(text)} characters and the limit is {limit}")


def store_copy(listing_doc: dict, locales: list[str]) -> dict[str, dict]:
    out = {}
    for locale in locales:
        loc_data = listing_doc.get("locales", {}).get(locale)
        if not loc_data:
            refuse(f"store-listing.toml has no [locales.\"{locale}\"]")
        ms = loc_data.get("microsoft", {})
        check_limit("microsoft.short-description", ms.get("short-description", ""))
        out[key(locale)] = {
            "title": ms.get("name"),
            "description": loc_data.get("description"),
            "short": ms.get("short-description"),
            "features": ms.get("features"),
            "keywords": ms.get("keywords"),
        }
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store-id", required=True, help="Partner Center's numeric Store ID for this product")
    p.add_argument("--version", required=True, help="e.g. 1.2.3, no leading v")
    p.add_argument("--package", required=True, type=Path, help="the .msix already fetched to disk")
    p.add_argument("--listing-toml", required=True, type=Path)
    p.add_argument("--notes-toml", required=True, type=Path)
    p.add_argument("--product-key", default="")
    p.add_argument("--locales", required=True, help="space-separated, ship.conf's spelling: en-us de-de")
    p.add_argument("--screenshots-dir", default="", type=Path)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    cli_ready()

    locales = args.locales.split()
    listing_doc = product_root(load_toml(args.listing_toml), args.product_key)
    notes_doc_full = load_toml(args.notes_toml)
    notes_doc = product_root(notes_doc_full, args.product_key)

    if notes_doc_full.get("version") != args.version:
        refuse(f"{args.notes_toml} says version = \"{notes_doc_full.get('version')}\" and this release is {args.version}; "
               "notes left from last time do not reach a listing")
    if not args.package.is_file():
        refuse(f"no package at {args.package}")

    app = app_get(args.store_id)
    name = app.get("PrimaryName", args.store_id)
    print(f"== {name} ({args.store_id})")

    if not app_submitted(app):
        refuse(f"{name} has never been submitted; the first submission is Partner Center's to make, age ratings and all")

    pkg_name = args.package.name

    if app_has_draft(app):
        draft = submission_get(args.store_id)
        if not doc_carries(draft, pkg_name):
            refuse(f"{name} has a submission open carrying {', '.join(p.get('FileName', '') for p in draft.get('ApplicationPackages', []))}; "
                   f"it is not this release. Finish it or delete it, because this step would otherwise change it")
        print(f"  a draft is already open, carrying {pkg_name}")
        if args.dry_run:
            # This draft predates this run — it may be someone's in-progress
            # work, not something to discard. A dry run reports and leaves it
            # exactly as found; only a draft this same run opened is undone.
            print("  --dry-run: leaving the existing draft open, unsubmitted")
            return
    else:
        published = submission_get(args.store_id)
        have = set(doc_locales(published))
        for locale in locales:
            k = key(locale)
            if k in have:
                continue
            doc_add_locale(published, k)
            print(f"  {k}: added, cloned from an existing language's shape")

        notes_map = {key(loc): (notes_doc.get("locales", {}).get(loc, {}) or {}).get("whats-new", "") for loc in locales}
        missing = doc_set_release_notes(published, notes_map)
        if missing:
            refuse(f"the listing is in {', '.join(missing)} and {args.notes_toml.name} says nothing in {', '.join(missing)}")

        missing = doc_set_listing(published, store_copy(listing_doc, locales))
        if missing:
            refuse(f"the listing is in {', '.join(missing)} and {args.listing_toml.name} has nothing for {', '.join(missing)}")
        for locale in locales:
            print(f"  {key(locale)}: the listing text is {args.listing_toml.name}'s")

        review_text = listing_doc.get("microsoft-review-notes", "")
        check_limit("microsoft-review-notes", review_text)
        if doc_set_certification_notes(published, review_text):
            print(f"  certification notes are {args.listing_toml.name}'s")
        else:
            print("  certification notes already say this")

        n_pictures = 0
        if args.screenshots_dir and Path(args.screenshots_dir).is_dir():
            sets = read_frames(Path(args.screenshots_dir))
            check_distinct(sets)
            staged_dir = Path(args.package).parent / "msstore-staging"
            staged_dir.mkdir(exist_ok=True)
            staged: dict[str, list[str]] = {}
            for locale, frames in sets.items():
                k = key(locale)
                if k not in have and k not in {key(l) for l in locales}:
                    warn(f"the listing is not in {k}, so those frames have nowhere to go")
                    continue
                names = []
                for f in frames:
                    name = f"{k}-{f.name}"
                    (staged_dir / name).write_bytes(f.read_bytes())
                    names.append(name)
                    n_pictures += 1
                staged[k] = names
            if n_pictures == 0 and sets:
                refuse(f"the set is in {', '.join(sorted(sets))} and the listing is in {', '.join(sorted(have))}, "
                       "so no frame has anywhere to go")
            retired = doc_set_images(published, staged)
            for was in retired:
                print(f"      replacing {was}")
            for locale, names in staged.items():
                print(f"  {locale}: {len(names)} frame(s) to go up")

        retired = doc_set_package(published, pkg_name)

        if args.dry_run:
            print(f"  --dry-run: would open a draft carrying {pkg_name} and {n_pictures} screenshot(s)")
            return

        submission_update(args.store_id, published)
        print("  a draft is open")
        for was in retired:
            print(f"      replacing {was}")

        draft = submission_get(args.store_id)
        waiting = doc_waiting(draft)
        if not waiting:
            refuse(f"the draft is not waiting for {pkg_name}; it carries "
                   f"{', '.join(p.get('FileName', '') for p in draft.get('ApplicationPackages', []))}")
        url = draft.get("FileUploadUrl")
        if not url:
            refuse(f"the draft came back with nowhere to send {', '.join(waiting)}")

        staged_dir = Path(args.package).parent / "msstore-staging"
        files = []
        for name in waiting:
            candidate = staged_dir / name
            files.append(args.package if name == pkg_name else candidate)
        zipped = Path(args.package).parent / "submission.zip"
        bundle(zipped, files)
        print(f"  sending {', '.join(waiting)}")
        push(url, zipped, tick=lambda: print(".", end="", flush=True))
        print()
        print("  the package is up")
        if n_pictures:
            print(f"  {n_pictures} screenshot(s) are up")

    # Both branches above return under --dry-run before anything is written:
    # the "no draft yet" path stops before opening one, and an existing draft
    # is left exactly as found. Nothing reaches here on a dry run.
    submission_publish(args.store_id)
    state = submission_state(args.store_id)
    if state == "Published":
        print(f"  ok: {name} {args.version} is in the Store")
    else:
        print(f"  ok: {name} {args.version} is with the Store: {state}")
        print("  certification takes hours to days; run this again to see where it got to")


if __name__ == "__main__":
    main()
