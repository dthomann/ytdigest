"""Resolve channel handles/URLs/Takeout CSV rows into canonical UC... channel IDs.

Used by `ytdigest import-channels <file>`. Three input shapes are supported:

1. A Google Takeout "subscriptions.csv" (header: Channel Id,Channel Url,Channel Title) —
   channel IDs are already canonical, no network needed.
2. A newline-separated file of bare UC... IDs, full channel URLs
   (youtube.com/channel/UC...), or @handles / handle URLs.
3. A single UC... ID, URL, or @handle passed directly (`add-channel`).

Resolving an @handle to a channel ID requires one YouTube Data API call
(channels.list?forHandle=...) and is therefore the only path that touches the network.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

import requests

UC_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


@dataclass
class ResolvedChannel:
    channel_id: str
    title: str | None = None
    handle: str | None = None


def is_channel_id(s: str) -> bool:
    return bool(UC_ID_RE.match(s.strip()))


def extract_handle_or_id(raw: str) -> tuple[str | None, str | None]:
    """Return (channel_id, handle) — exactly one is set — from a URL/handle/bare ID string."""
    s = raw.strip()
    if not s:
        return None, None

    m = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", s)
    if m:
        return m.group(1), None

    if is_channel_id(s):
        return s, None

    m = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", s)
    if m:
        return None, "@" + m.group(1)

    if s.startswith("@"):
        return None, s

    return None, None


def resolve_handle(handle: str, api_key: str) -> ResolvedChannel:
    """Resolve an @handle to a channel ID via the YouTube Data API."""
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "forHandle": handle.lstrip("@"), "key": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    if not items:
        raise ValueError(f"Could not resolve handle {handle!r} to a channel ID")
    item = items[0]
    return ResolvedChannel(
        channel_id=item["id"], title=item["snippet"].get("title"), handle=handle
    )


def parse_takeout_csv(path: Path) -> list[ResolvedChannel]:
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cid = (row.get("Channel Id") or "").strip()
            if is_channel_id(cid):
                out.append(ResolvedChannel(channel_id=cid, title=(row.get("Channel Title") or "").strip() or None))
    return out


def resolve_file(path: str | Path, api_key: str | None = None) -> list[ResolvedChannel]:
    """Resolve every entry in a Takeout CSV or newline-separated list file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    if "Channel Id" in text.splitlines()[0] if text.splitlines() else False:
        return parse_takeout_csv(path)

    out: list[ResolvedChannel] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cid, handle = extract_handle_or_id(line)
        if cid:
            out.append(ResolvedChannel(channel_id=cid))
        elif handle:
            if not api_key:
                raise ValueError(
                    f"Cannot resolve handle {handle!r} without YOUTUBE_API_KEY (line: {line!r})"
                )
            out.append(resolve_handle(handle, api_key))
        else:
            raise ValueError(f"Could not parse channel entry: {line!r}")
    return out


def resolve_one(raw: str, api_key: str | None = None) -> ResolvedChannel:
    cid, handle = extract_handle_or_id(raw)
    if cid:
        return ResolvedChannel(channel_id=cid)
    if handle:
        if not api_key:
            raise ValueError(f"Cannot resolve handle {handle!r} without YOUTUBE_API_KEY")
        return resolve_handle(handle, api_key)
    raise ValueError(f"Could not parse channel entry: {raw!r}")
