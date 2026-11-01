#!/usr/bin/env python3
"""Build a size-capped XMLTV EPG, filtered to a fixed set of channel ids.

Why this exists
----------------
Personal fork of the "merge upstream EPG feeds into one" idea, adapted to a
single upstream EPG source and a playlist that must never be committed to
this repo (it embeds per-account stream auth tokens in its catchup/stream
URLs). Instead of fetching the playlist live, the channel ids it needs are
committed here as a flat list (channel_ids.txt) with no stream URLs or
tokens attached. Regenerate that list locally with update_channel_ids.py
whenever the playlist's channel lineup changes.

What it does
------------
1. Loads the channel ids to keep from channel_ids.txt.
2. Fetches the upstream EPG, keeps only channels in that list.
3. Trims to fit a size ceiling, reformats one-element-per-line.
4. Refuses to publish a near-empty or decoy-looking result (safety nets).
"""
import sys
import urllib.request
import gzip
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

# Keep the published file comfortably under a size that's proven to load on
# constrained media-player XML parsers.
SIZE_CAP = int(19.5 * 1024 * 1024)

# Upstream EPG source, read from the playlist's own url-tvg header. Contains
# no per-account tokens (unlike the playlist itself), so it's safe to commit.
EPG_SRC = "https://0i4neux01.charandom.blog/epg/kai-epg.xml"

CHANNEL_IDS_FILE = "channel_ids.txt"

# Safety nets (see main()): abort rather than publish a broken/decoy feed.
MIN_CHANNELS = 70       # last observed match was 98/100 channel ids
MAX_TITLE_SHARE = 0.5   # one title must not dominate like this


def fetch(url, tries=3, timeout=240):
    """Download a URL as text, retrying and transparently gunzipping."""
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "epg-merger"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                encoded_gzip = r.info().get("Content-Encoding") == "gzip"
            if encoded_gzip or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - network is allowed to flake
            last = e
    print(f"  WARN: failed to fetch {url}: {last}", file=sys.stderr)
    return None


def repaired_root(text):
    """Parse XMLTV text, repairing truncation by closing a dangling <tv>."""
    if text is None:
        return None
    if "</tv>" not in text:
        cut = max(text.rfind("</programme>"), text.rfind("</channel>"))
        if cut != -1:
            end = text.index(">", cut) + 1
            text = text[:end] + "\n</tv>"
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        print(f"  WARN: parse error after repair: {e}", file=sys.stderr)
        return None


def load_channel_ids():
    with open(CHANNEL_IDS_FILE, encoding="utf-8") as fh:
        return {
            line.strip()
            for line in fh
            if line.strip() and not line.startswith("#")
        }


def trim_to_fit(merged):
    """Trim the merged guide so it stays under SIZE_CAP, keeping as many forward
    days as fit. Drops stale past programmes and bulky programme poster icons
    (channel logos come from the playlist, so they aren't needed here)."""
    channels = [e for e in merged if e.tag == "channel"]
    programmes = [e for e in merged if e.tag == "programme"]
    for pr in programmes:
        for ic in pr.findall("icon"):
            pr.remove(ic)

    jst = datetime.now(timezone.utc) + timedelta(hours=9)  # Japanese broadcast day
    lo = (jst - timedelta(days=1)).strftime("%Y%m%d")

    def build(hi):
        tv = ET.Element("tv", merged.attrib)
        for c in channels:
            tv.append(c)
        kept = 0
        for pr in programmes:
            if lo <= pr.get("start", "")[:8] <= hi:
                tv.append(pr)
                kept += 1
        return tv, kept

    # Take the largest forward window that fits; self-adapts to daily density.
    for fwd in range(8, 1, -1):
        hi = (jst + timedelta(days=fwd)).strftime("%Y%m%d")
        tv, kept = build(hi)
        size = len(ET.tostring(tv, encoding="utf-8"))
        if size <= SIZE_CAP:
            print(f"  window {lo}..+{fwd}d: {kept} programmes, {size/1e6:.1f} MB (fits)")
            return tv
        print(f"  window {lo}..+{fwd}d: {size/1e6:.1f} MB over cap, trying shorter")
    tv, kept = build((jst + timedelta(days=1)).strftime("%Y%m%d"))
    print(f"  fallback {lo}..+1d: {kept} programmes")
    return tv


def main():
    needed = load_channel_ids()
    print(f"tracking {len(needed)} channel ids from {CHANNEL_IDS_FILE}")

    root = repaired_root(fetch(EPG_SRC))
    if root is None:
        print("ERROR: upstream EPG unavailable — refusing to overwrite the live EPG",
              file=sys.stderr)
        sys.exit(1)

    out = ET.Element("tv", {"generator-info-name": "personal merged EPG"})
    n_ch = n_pr = 0
    titles = {}
    for ch in root.findall("channel"):
        if ch.get("id") in needed:
            out.append(ch)
            n_ch += 1
    for pr in root.findall("programme"):
        if pr.get("channel") in needed:
            out.append(pr)
            n_pr += 1
            t = pr.findtext("title") or ""
            titles[t] = titles.get(t, 0) + 1
    print(f"merged (full): {n_ch} channels, {n_pr} programmes")

    # Safety net: last observed match was 98/100 playlist channel ids. If far
    # fewer show up, upstream likely changed channel ids or is serving a
    # broken file — abort so the Action keeps the last-good published file.
    if n_ch < MIN_CHANNELS:
        print(f"ERROR: only {n_ch} channels (< {MIN_CHANNELS}) — upstream EPG or "
              f"ids likely changed; refusing to overwrite the live EPG", file=sys.stderr)
        sys.exit(1)

    # Safety net: a real guide has mostly distinct programme titles. A decoy or
    # placeholder feed (e.g. an anti-scraping notice) repeats one title across
    # every slot — catch that shape even if channel/programme counts look fine.
    if titles and n_pr:
        top_title, top_count = max(titles.items(), key=lambda kv: kv[1])
        if top_count / n_pr > MAX_TITLE_SHARE:
            print(f"ERROR: {top_count}/{n_pr} programmes share one title "
                  f"({top_title[:60]!r}) — likely a placeholder/notice feed, not "
                  f"real listings; refusing to overwrite the live EPG", file=sys.stderr)
            sys.exit(1)

    final = trim_to_fit(out)

    # Serialize one element per line: ElementTree's default output is a single
    # multi-megabyte line, which some player-side XML parsers reject.
    with open("epg-merged.xml", "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        attrs = "".join(f' {k}="{v}"' for k, v in final.attrib.items())
        f.write(f"<tv{attrs}>\n")
        for el in final:
            f.write(ET.tostring(el, encoding="unicode"))
            f.write("\n")
        f.write("</tv>\n")
    print(f"WROTE epg-merged.xml: {len(final.findall('channel'))} channels, "
          f"{len(final.findall('programme'))} programmes")


if __name__ == "__main__":
    main()
