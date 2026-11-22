"""Steam Workshop client with a local on-disk cache (no API key needed)."""
from __future__ import annotations

import html
import json
import os
import re
import time
import uuid
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
COLLECTION_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
PROFILE_URL = "https://steamcommunity.com/profiles/{creator}?xml=1"
BATCH = 100
MAX_COLLECTION_ITEMS = 500
TTL = 24 * 3600
AUTHOR_TTL = 7 * 24 * 3600


def parse_collection_id(value):
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"[?&]id=(\d+)", text) or re.match(r"^(\d{4,})$", text)
    return match.group(1) if match else None


def _strip_bbcode(text):
    text = re.sub(r"\[/?[^\]]+\]", "", text or "")
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _post(url, fields):
    body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "StellarisModManager/0.7"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _clean_item(raw):
    tags = raw.get("tags") or []
    return {
        "id": str(raw.get("publishedfileid") or ""),
        "result": raw.get("result"),
        "title": raw.get("title") or "",
        "description": _strip_bbcode(raw.get("description"))[:4000],
        "creator": str(raw.get("creator") or ""),
        "time_created": raw.get("time_created"),
        "time_updated": raw.get("time_updated"),
        "subscriptions": raw.get("subscriptions") or 0,
        "favorited": raw.get("favorited") or 0,
        "views": raw.get("views") or 0,
        "file_size": raw.get("file_size") or 0,
        "visibility": raw.get("visibility"),
        "banned": raw.get("banned"),
        "preview_url": raw.get("preview_url") or "",
        "tags": [tag.get("tag") for tag in tags if isinstance(tag, dict) and tag.get("tag")],
    }


class WorkshopService:
    def __init__(self, data_dir, ttl=TTL):
        self.path = data_dir / "workshop_cache.json"
        self.ttl = ttl
        self.items = {}
        self.authors = {}
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = payload.get("items") or {}
            self.authors = payload.get("authors") or {}
        except (OSError, ValueError):
            self.items = {}
            self.authors = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps({"items": self.items, "authors": self.authors}, ensure_ascii=False),
                            encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- authors
    def _fetch_author(self, creator):
        request = urllib.request.Request(
            PROFILE_URL.format(creator=creator),
            headers={"User-Agent": "StellarisModManager/0.7"},
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return ""
        return (root.findtext("steamID") or "").strip()

    def author_name(self, creator, force=False):
        creator = str(creator or "")
        if not creator:
            return ""
        entry = self.authors.get(creator)
        if entry and not force and time.time() - (entry.get("fetched") or 0) < AUTHOR_TTL:
            return entry.get("name") or ""
        previous = (self.authors.get(creator) or {}).get("name") or ""
        try:
            name = self._fetch_author(creator) or previous
        except (OSError, ValueError):
            name = previous
        self.authors[creator] = {"name": name, "fetched": time.time()}
        return name

    def resolve_authors(self, creators, force=False, workers=6):
        wanted = sorted({str(item) for item in creators if item})
        if not wanted:
            return {}
        now = time.time()
        pending = [item for item in wanted
                   if force or item not in self.authors
                   or now - (self.authors[item].get("fetched") or 0) >= AUTHOR_TTL]
        if pending:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda cid: self.author_name(cid, force=True), pending))
            self._save()
        return {item: (self.authors.get(item) or {}).get("name") or "" for item in wanted}

    # ---------------------------------------------------------------- details
    def summary(self, steam_id):
        entry = self.items.get(str(steam_id))
        if not entry:
            return None
        data = entry.get("data") or {}
        author = self.authors.get(str(data.get("creator") or ""), {})
        return {
            "title": data.get("title") or "",
            "author": author.get("name") or "",
            "author_url": f"https://steamcommunity.com/profiles/{data.get('creator')}" if data.get("creator") else "",
            "subscriptions": data.get("subscriptions") or 0,
            "time_created": data.get("time_created") or 0,
            "time_updated": data.get("time_updated"),
            "tags": data.get("tags") or [],
            "description": _strip_bbcode(data.get("description") or "")[:400],
            "creator": data.get("creator") or "",
        }

    def details(self, ids, force=False):
        wanted = [str(item) for item in ids if item]
        if not wanted:
            return {}
        now = time.time()
        if not force:
            wanted = [item for item in wanted
                      if item not in self.items or now - (self.items[item].get("fetched") or 0) > self.ttl]
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            fields = {"itemcount": len(chunk)}
            for index, item in enumerate(chunk):
                fields[f"publishedfileids[{index}]"] = item
            try:
                payload = _post(DETAILS_URL, fields)
            except (OSError, ValueError):
                continue
            for raw in (payload.get("response") or {}).get("publishedfiledetails") or []:
                item = _clean_item(raw)
                if item["id"]:
                    self.items[item["id"]] = {"fetched": now, "data": item}
        self._save()
        return {item: self.items[item]["data"] for item in ids if str(item) in self.items}

    def creators(self, ids):
        return {
            (self.items.get(str(item)) or {}).get("data", {}).get("creator")
            for item in ids
            if (self.items.get(str(item)) or {}).get("data", {}).get("creator")
        }

    def collection(self, raw):
        collection_id = parse_collection_id(raw)
        if not collection_id:
            raise ValueError("无法识别合集 ID，请粘贴创意工坊合集链接或数字 ID。")
        try:
            payload = _post(COLLECTION_URL, {
                "collectioncount": 1,
                "publishedfileids[0]": collection_id,
            })
        except OSError as exc:
            raise ValueError(f"无法访问 Steam 接口：{exc}") from exc
        entries = (payload.get("response") or {}).get("collectiondetails") or []
        if not entries:
            raise ValueError("Steam 没有返回这个合集，请检查 ID。")
        entry = entries[0]
        if entry.get("result") != 1:
            raise ValueError("该 ID 不是公开的创意工坊合集。")
        children = entry.get("children") or []
        ids = [str(child.get("publishedfileid")) for child in children][:MAX_COLLECTION_ITEMS]
        details = self.details(ids + [collection_id])
        meta = details.get(collection_id) or self.items.get(collection_id, {}).get("data") or {}
        return {
            "id": collection_id,
            "title": (meta or {}).get("title") or f"合集 {collection_id}",
            "total": len(ids),
            "children": ids,
            "details": details,
        }


