#!/usr/bin/env python3
"""App Store Connect prep: open or reuse the version record, push the
listing text and release notes, and confirm (optionally place) screenshots.

This is CI's half of a Mac App Store release. It never signs anything,
never uploads a build, and never submits for review — those three need the
Apple Distribution certificate, which lives on one Mac and stays there. What
is here is everything else: the version, the copy, the notes, the pictures.

The API sequence (endpoints, JSON shapes, the two-record listing split
between the application and the version, the locale tag/key split between
en-US and en-us, the reserve/upload/commit screenshot dance) is ported from
excelano/ship's internal/appstore package, which had already found the real
gotchas the hard way. This script carries no authority from ship's own
design — only that domain knowledge.

Author: David M. Anderson
Built with AI assistance (Claude, Anthropic)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import jwt  # PyJWT, with the cryptography extra for ES256

API_BASE = "https://api.appstoreconnect.apple.com/v1/"
PLATFORM = "MAC_OS"
DESKTOP = "APP_DESKTOP"
MOST_SCREENSHOTS = 10
# Apple's published macOS listing frame sizes. 1440x900 and 2880x1800 are
# what this fleet's live listings actually use; the other two are Apple's
# word for it. A Windows frame (1366x768) handed to this lane is refused by
# Apple after the build is already signed and uploaded, so it is caught here
# instead.
SHAPES = {(1280, 800), (1440, 900), (2560, 1600), (2880, 1800)}

# The limits packaging/store-listing.toml's own header documents, counted in
# characters. Enforced here so a release stops before the push rather than
# failing partway through it.
LIMITS = {
    "apple.name": 30,
    "apple.subtitle": 30,
    "apple.promotional-text": 170,
    "apple.keywords": 100,
    "description": 4000,
    "apple-review-notes": 4000,
    "microsoft-review-notes": 4000,
}


def err(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"::warning::{msg}")


def refuse(msg: str) -> None:
    err(msg)
    sys.exit(1)


def tag(locale: str) -> str:
    """The language as App Store Connect keys it: en-US, de-DE."""
    lang, _, region = locale.partition("-")
    return f"{lang}-{region.upper()}" if region else locale


class Client:
    """An authenticated conversation with App Store Connect. One token is
    minted at construction and reused for the run; a prep run is minutes,
    not the hours a local release with a Mac build in the middle can be, so
    there is no need to re-mint mid-run the way ship's long-lived client
    does."""

    def __init__(self, key_id: str, issuer_id: str, private_key_pem: str):
        now = int(time.time())
        self.token = jwt.encode(
            {"iss": issuer_id, "iat": now, "exp": now + 19 * 60, "aud": "appstoreconnect-v1"},
            private_key_pem,
            algorithm="ES256",
            headers={"kid": key_id},
        )

    def _do(self, method: str, path: str, body: dict | None = None):
        url = API_BASE + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                said = resp.read()
        except urllib.error.HTTPError as e:
            said = e.read()
            raise RuntimeError(f"{method} {path}: {self._apple_said(e.code, said)}") from None
        if not said:
            return None
        return json.loads(said)

    @staticmethod
    def _apple_said(code: int, body: bytes) -> str:
        try:
            doc = json.loads(body)
            parts = [e.get("detail") or e.get("title", "") for e in doc.get("errors", [])]
            parts = [p for p in parts if p]
            if parts:
                return "; ".join(parts)
        except (json.JSONDecodeError, AttributeError):
            pass
        text = body.decode(errors="replace").strip()
        return f"HTTP {code}: {text}" if text else f"HTTP {code}, and nothing said"

    def get(self, path):
        return self._do("GET", path)

    def post(self, path, body):
        return self._do("POST", path, body)

    def patch(self, path, body):
        return self._do("PATCH", path, body)

    def delete(self, path):
        self._do("DELETE", path)

    # ---- application -----------------------------------------------------

    def app(self, bundle_id: str) -> dict:
        doc = self.get(f"apps?filter[bundleId]={urllib_quote(bundle_id)}")
        data = doc.get("data") or []
        if not data:
            refuse(f"App Store Connect has no application with the identifier {bundle_id}; "
                    "the record is made once, in the browser")
        d = data[0]
        return {"id": d["id"], "name": d["attributes"]["name"]}

    # ---- versions ----------------------------------------------------------

    OPEN_STATES = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED",
                    "METADATA_REJECTED", "INVALID_BINARY"}
    GONE_STATES = {"REPLACED_WITH_NEW_VERSION", "REMOVED_FROM_SALE"}
    LIVE_STATE = "READY_FOR_SALE"

    def versions(self, app_id: str) -> list[dict]:
        doc = self.get(f"apps/{app_id}/appStoreVersions?filter[platform]={PLATFORM}&limit=20")
        return [{"id": d["id"], "version": d["attributes"]["versionString"], "state": d["attributes"]["appStoreState"]}
                for d in doc.get("data") or []]

    def underway(self, versions: list[dict]) -> dict | None:
        for v in versions:
            if v["state"] == self.LIVE_STATE or v["state"] in self.GONE_STATES:
                continue
            return v
        return None

    def selling(self, versions: list[dict]) -> dict | None:
        for v in versions:
            if v["state"] == self.LIVE_STATE:
                return v
        return None

    def new_version(self, app_id: str, version: str) -> dict:
        body = {"data": {"type": "appStoreVersions",
                          "attributes": {"platform": PLATFORM, "versionString": version},
                          "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}}
        doc = self.post("appStoreVersions", body)
        d = doc["data"]
        return {"id": d["id"], "version": d["attributes"]["versionString"], "state": d["attributes"]["appStoreState"]}

    def set_version_string(self, version_id: str, version: str) -> None:
        body = {"data": {"type": "appStoreVersions", "id": version_id, "attributes": {"versionString": version}}}
        self.patch(f"appStoreVersions/{version_id}", body)

    # ---- application-level info (name, subtitle, privacy) -----------------

    def app_info_id(self, app_id: str) -> str | None:
        """The editable application-record id, or None where nothing is
        under way — Apple opens this alongside a version under way, not
        before."""
        doc = self.get(f"apps/{app_id}/appInfos?limit=50")
        for d in doc.get("data") or []:
            if d["attributes"]["state"] in self.OPEN_STATES:
                return d["id"]
        return None

    def info_listings(self, info_id: str) -> dict[str, str]:
        doc = self.get(f"appInfos/{info_id}/appInfoLocalizations?limit=50")
        return {d["attributes"]["locale"]: d["id"] for d in doc.get("data") or []}

    def set_info(self, named_id: str, info: dict) -> None:
        self.patch(f"appInfoLocalizations/{named_id}", {"data": {"type": "appInfoLocalizations", "id": named_id, "attributes": info}})

    def new_info_listing(self, info_id: str, locale: str, info: dict) -> str:
        attrs = dict(info, locale=locale)
        body = {"data": {"type": "appInfoLocalizations", "attributes": attrs,
                          "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info_id}}}}}
        return self.post("appInfoLocalizations", body)["data"]["id"]

    # ---- version-level listing (description, keywords, promo text) -------

    def listings(self, version_id: str) -> dict[str, str]:
        doc = self.get(f"appStoreVersions/{version_id}/appStoreVersionLocalizations?limit=50")
        return {d["attributes"]["locale"]: d["id"] for d in doc.get("data") or []}

    def set_copy(self, listing_id: str, copy: dict) -> None:
        self.patch(f"appStoreVersionLocalizations/{listing_id}", {"data": {"type": "appStoreVersionLocalizations", "id": listing_id, "attributes": copy}})

    def new_listing(self, version_id: str, locale: str, copy: dict) -> str:
        attrs = dict(copy, locale=locale)
        body = {"data": {"type": "appStoreVersionLocalizations", "attributes": attrs,
                          "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}}
        try:
            return self.post("appStoreVersionLocalizations", body)["data"]["id"]
        except RuntimeError as e:
            if "already exists" not in str(e):
                raise
            for loc, lid in self.listings(version_id).items():
                if loc == locale:
                    self.set_copy(lid, copy)
                    return lid
            raise

    def set_whats_new(self, version_id: str, notes: dict[str, str]) -> tuple[list[str], list[str]]:
        wrote, missing = [], []
        doc = self.get(f"appStoreVersions/{version_id}/appStoreVersionLocalizations?limit=50")
        for d in doc.get("data") or []:
            locale = d["attributes"]["locale"]
            if locale not in notes:
                missing.append(locale)
                continue
            if d["attributes"].get("whatsNew") != notes[locale]:
                self.patch(f"appStoreVersionLocalizations/{d['id']}",
                           {"data": {"type": "appStoreVersionLocalizations", "id": d["id"], "attributes": {"whatsNew": notes[locale]}}})
            wrote.append(locale)
        return sorted(wrote), sorted(missing)

    # ---- review notes -------------------------------------------------------

    def review_notes(self, version_id: str) -> tuple[str, str]:
        doc = self.get(f"appStoreVersions/{version_id}/appStoreReviewDetail")
        d = doc["data"]
        return d["id"], d["attributes"].get("notes", "")

    def set_review_notes(self, version_id: str, text: str) -> bool:
        rid, notes = self.review_notes(version_id)
        if notes == text:
            return False
        self.patch(f"appStoreReviewDetails/{rid}", {"data": {"type": "appStoreReviewDetails", "id": rid, "attributes": {"notes": text}}})
        return True

    # ---- screenshots --------------------------------------------------------

    def find_set(self, listing_id: str, display: str) -> str | None:
        doc = self.get(f"appStoreVersionLocalizations/{listing_id}/appScreenshotSets?limit=50")
        for d in doc.get("data") or []:
            if d["attributes"]["screenshotDisplayType"] == display:
                return d["id"]
        return None

    def make_set(self, listing_id: str, display: str) -> str:
        found = self.find_set(listing_id, display)
        if found:
            return found
        body = {"data": {"type": "appScreenshotSets", "attributes": {"screenshotDisplayType": display},
                          "relationships": {"appStoreVersionLocalization": {"data": {"type": "appStoreVersionLocalizations", "id": listing_id}}}}}
        return self.post("appScreenshotSets", body)["data"]["id"]

    def held(self, set_id: str) -> list[dict]:
        doc = self.get(f"appScreenshotSets/{set_id}/appScreenshots?limit=50")
        return [{"id": d["id"], "name": d["attributes"]["fileName"]} for d in doc.get("data") or []]

    def add_shot(self, set_id: str, path: Path) -> str:
        size = path.stat().st_size
        body = {"data": {"type": "appScreenshots", "attributes": {"fileSize": size, "fileName": path.name},
                          "relationships": {"appScreenshotSet": {"data": {"type": "appScreenshotSets", "id": set_id}}}}}
        made = self.post("appScreenshots", body)
        shot_id = made["data"]["id"]
        ops = made["data"]["attributes"].get("uploadOperations") or []
        if not ops:
            refuse(f"{path.name}: Connect reserved the screenshot and gave nowhere to put it")
        raw = path.read_bytes()
        for op in ops:
            offset, length = op["offset"], op["length"]
            chunk = raw[offset:offset + length]
            req = urllib.request.Request(op["url"], data=chunk, method=op["method"])
            for h in op.get("requestHeaders") or []:
                req.add_header(h["name"], h["value"])
            with urllib.request.urlopen(req, timeout=120):
                pass
        checksum = hashlib.md5(raw).hexdigest()
        self.patch(f"appScreenshots/{shot_id}", {"data": {"type": "appScreenshots", "id": shot_id,
                    "attributes": {"uploaded": True, "sourceFileChecksum": checksum}}})
        return shot_id

    def drop_shot(self, shot_id: str) -> None:
        self.delete(f"appScreenshots/{shot_id}")

    def order_shots(self, set_id: str, ids: list[str]) -> None:
        data = [{"type": "appScreenshots", "id": i} for i in ids]
        self.patch(f"appScreenshotSets/{set_id}/relationships/appScreenshots", {"data": data})

    def wait_delivered(self, set_id: str, timeout_s: int = 600) -> None:
        deadline = time.time() + timeout_s
        while True:
            doc = self.get(f"appScreenshotSets/{set_id}/appScreenshots?limit=50")
            waiting = 0
            for d in doc.get("data") or []:
                state = d["attributes"]["assetDeliveryState"]["state"]
                if state == "COMPLETE":
                    continue
                if state == "FAILED":
                    errs = d["attributes"]["assetDeliveryState"].get("errors") or []
                    said = "; ".join(e.get("description", "") for e in errs) or "and said nothing about why"
                    refuse(f"{d['attributes']['fileName']} was refused: {said}")
                waiting += 1
            if waiting == 0:
                return
            if time.time() > deadline:
                refuse(f"{waiting} screenshot(s) still processing after {timeout_s}s")
            time.sleep(5)

    def carrying(self, version_id: str) -> dict[str, int]:
        out = {}
        for locale, listing_id in self.listings(version_id).items():
            set_id = self.find_set(listing_id, DESKTOP)
            out[locale] = len(self.held(set_id)) if set_id else 0
        return out

    def place(self, listing_id: str, frames: list[Path]) -> None:
        set_id = self.make_set(listing_id, DESKTOP)
        held = self.held(set_id)
        if len(held) + len(frames) > MOST_SCREENSHOTS:
            refuse(f"the set holds {len(held)} and {len(frames)} more would pass Apple's limit of {MOST_SCREENSHOTS}; "
                   "the old ones have to come down first")
        added = [self.add_shot(set_id, f) for f in frames]
        for h in held:
            self.drop_shot(h["id"])
        self.order_shots(set_id, added)
        self.wait_delivered(set_id)


def urllib_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ---- screenshot files on disk --------------------------------------------

def read_frames(directory: Path) -> dict[str, list[Path]]:
    """dist/screenshots/<Apple-tag>/<NN-name>.png, one subdirectory per
    language, named in Apple's spelling because that is the store the frames
    were first built for (see locale.go's own note: the Microsoft lane
    converts from this same layout, and getting the direction backwards once
    already shipped a submission with no pictures)."""
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


def frame_size(path: Path) -> tuple[int, int]:
    # PNG: 8-byte signature, then an IHDR chunk whose first 8 bytes after the
    # length+type are width and height, big-endian. Read the header rather
    # than decoding the image, since only the two dimensions are wanted.
    with open(path, "rb") as f:
        data = f.read(24)
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        refuse(f"{path}: not a PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def check_fit(sets: dict[str, list[Path]]) -> None:
    bad = []
    for locale, frames in sets.items():
        for f in frames:
            w, h = frame_size(f)
            if (w, h) not in SHAPES:
                bad.append(f"{locale}/{f.name} is {w}x{h}")
    if bad:
        want = ", ".join(f"{w}x{h}" for w, h in sorted(SHAPES))
        refuse(", ".join(sorted(bad)) + f"; a Mac App Store frame is {want}, and 1366x768 is what the Windows recipes write")


def check_distinct(sets: dict[str, list[Path]]) -> None:
    seen: dict[str, list[str]] = {}
    for locale in sorted(sets):
        for f in sets[locale]:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            seen.setdefault(digest, []).append(f"{locale}/{f.name}")
    dupes = [" = ".join(names) for names in seen.values() if len(names) > 1]
    if dupes:
        refuse("the same picture more than once: " + "; ".join(dupes) +
               ". A frame that repeats is a frame whose actions changed nothing, "
               "and a listing of a window at rest is what Apple refuses under 2.3.3")


# ---- repo data ------------------------------------------------------------

def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def check_limit(name: str, text: str) -> None:
    limit = LIMITS.get(name)
    if limit and len(text) > limit:
        refuse(f"{name} is {len(text)} characters and the limit is {limit}")


def product_root(doc: dict, product_key: str) -> dict:
    """A repo shipping one application keys store-listing.toml and
    release-notes.toml at the top level. One shipping several (odox: xodt,
    xods, xodp) nests each under [products.<key>], the way
    packaging/macos/identity.toml and packaging/windows/identity.psd1 key
    the same applications. release-notes.toml's version stays top-level
    either way — one release number for the whole repo — so this is never
    applied to it, only to where locales/apple-review-notes/
    microsoft-review-notes are read from."""
    if not product_key:
        return doc
    try:
        return doc["products"][product_key]
    except KeyError:
        refuse(f"no [products.{product_key}] table")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle-id", required=True)
    p.add_argument("--version", required=True, help="e.g. 1.2.3, no leading v")
    p.add_argument("--key-id", required=True)
    p.add_argument("--issuer-id", required=True)
    p.add_argument("--private-key-file", required=True, help="path to the .p8 key")
    p.add_argument("--listing-toml", required=True, type=Path)
    p.add_argument("--notes-toml", required=True, type=Path)
    p.add_argument("--product-key", default="", help="the [products.<key>] table, for a repo shipping more than one application")
    p.add_argument("--locales", required=True, help="space-separated, ship.conf's spelling: en-us de-de")
    p.add_argument("--screenshots-dir", default="", type=Path)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    locales = args.locales.split()
    listing_doc = product_root(load_toml(args.listing_toml), args.product_key)
    notes_doc_full = load_toml(args.notes_toml)
    notes_doc = product_root(notes_doc_full, args.product_key)

    if notes_doc_full.get("version") != args.version:
        refuse(f"{args.notes_toml} says version = \"{notes_doc_full.get('version')}\" and this release is {args.version}; "
               "notes left from last time do not reach a listing")

    key_pem = Path(args.private_key_file).read_text()
    c = Client(args.key_id, args.issuer_id, key_pem)

    app = c.app(args.bundle_id)
    print(f"== {app['name']} ({args.bundle_id})")

    # ---- open or reuse the version -----------------------------------------
    versions = c.versions(app["id"])
    open_v = c.underway(versions)
    if open_v is None:
        if args.dry_run:
            print(f"  --dry-run: would open version {args.version}")
            version = {"id": "(dry-run)", "version": args.version}
        else:
            version = c.new_version(app["id"], args.version)
            print(f"  opened {version['version']}")
    elif open_v["state"] not in c.OPEN_STATES:
        refuse(f"{app['name']} has {open_v['version']} {open_v['state'].lower().replace('_', ' ')}; "
               "it is with Apple and this step would change what they are looking at")
    elif open_v["version"] == args.version:
        version = open_v
        print(f"  {version['version']} is already open")
    else:
        if not args.dry_run:
            c.set_version_string(open_v["id"], args.version)
        print(f"  {open_v['version']} was open and is now {args.version}")
        version = {"id": open_v["id"], "version": args.version}

    # ---- listing text (app-level + version-level) --------------------------
    info_id = c.app_info_id(app["id"]) if not args.dry_run else None
    named = {}
    if info_id:
        named = c.info_listings(info_id)
    have_listing = {} if args.dry_run else c.listings(version["id"])

    wrote_new = []
    for locale in locales:
        loc_data = listing_doc.get("locales", {}).get(locale)
        if not loc_data:
            refuse(f"{args.listing_toml} has no [locales.\"{locale}\"]")
        apple = loc_data.get("apple", {})
        for field, key in (("name", "apple.name"), ("subtitle", "apple.subtitle"),
                            ("promotional-text", "apple.promotional-text"), ("keywords", "apple.keywords")):
            if field in apple:
                check_limit(key, apple[field])
        check_limit("description", loc_data.get("description", ""))

        t = tag(locale)
        if args.dry_run:
            continue

        info = {k: v for k, v in {"name": apple.get("name"), "subtitle": apple.get("subtitle"),
                                   "privacyPolicyUrl": loc_data.get("privacy")}.items() if v}
        if t in named:
            c.set_info(named[t], info)
        elif info_id:
            c.new_info_listing(info_id, t, info)

        copy = {k: v for k, v in {
            "description": loc_data.get("description"),
            "keywords": apple.get("keywords"),
            "promotionalText": apple.get("promotional-text"),
            "marketingUrl": loc_data.get("website"),
            "supportUrl": loc_data.get("support"),
        }.items() if v}
        if t in have_listing:
            c.set_copy(have_listing[t], copy)
        else:
            c.new_listing(version["id"], t, copy)
            wrote_new.append(t)
        print(f"  {t}: the listing text is {args.listing_toml.name}'s" + (" (new)" if t in wrote_new else ""))
    if not info_id and not args.dry_run:
        warn(f"{app['name']} has no editable application record, so the name and the subtitle stay as they are")

    # ---- what's new + review notes -----------------------------------------
    review_text = listing_doc.get("apple-review-notes", "")
    check_limit("apple-review-notes", review_text)
    notes_map = {tag(loc): (notes_doc.get("locales", {}).get(loc, {}) or {}).get("whats-new", "") for loc in locales}
    if not args.dry_run:
        wrote, missing = c.set_whats_new(version["id"], notes_map)
        if missing:
            warn(f"the listing is in {', '.join(missing)} and {args.notes_toml.name} says nothing in {', '.join(missing)}")
        print(f"  what's new is {args.notes_toml.name}'s, for {', '.join(wrote)}")
        if c.set_review_notes(version["id"], review_text):
            print(f"  the reviewer's notes are {args.listing_toml.name}'s")
        else:
            print("  the reviewer's notes already say this")

    # ---- screenshots: confirm, and place if a set was given ----------------
    carrying = {} if args.dry_run else c.carrying(version["id"])
    bare = [loc for loc in (tag(l) for l in locales) if carrying.get(loc, 0) == 0]

    if args.screenshots_dir and Path(args.screenshots_dir).is_dir():
        sets = read_frames(Path(args.screenshots_dir))
        check_fit(sets)
        check_distinct(sets)
        for locale, frames in sets.items():
            listing_id = have_listing.get(locale)
            if not listing_id:
                warn(f"{app['name']} has no listing in {locale}, so those frames have nowhere to go")
                continue
            if not args.dry_run:
                c.place(listing_id, frames)
                print(f"  {locale}: placed {len(frames)} frame(s)")
            else:
                print(f"  --dry-run: would place {len(frames)} frame(s) in {locale}")
        bare = [loc for loc in bare if loc not in sets]

    if bare and not args.dry_run:
        refuse(f"{app['name']} {version['version']} would list {' and '.join(bare)}, which carries no screenshots; "
               "Apple refuses a submission whose language has none")
    elif bare:
        print(f"  --dry-run: would refuse for missing screenshots in {', '.join(bare)}")

    print(f"\n{app['name']} {version['version']} is prepared. "
          "Signing, upload and submission happen on the Mac.")


if __name__ == "__main__":
    main()
