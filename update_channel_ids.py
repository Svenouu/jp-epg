#!/usr/bin/env python3
"""Regenerate channel_ids.txt from a local playlist.m3u (never committed).

Run this locally whenever your playlist's channel lineup changes, then
commit the updated channel_ids.txt. This script only reads tvg-id values —
it never prints or stores the playlist's stream URLs or auth tokens.
"""
import re
import sys

PLAYLIST_FILE = "playlist.m3u"
OUTPUT_FILE = "channel_ids.txt"


def main():
    try:
        with open(PLAYLIST_FILE, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        print(f"ERROR: {PLAYLIST_FILE} not found in current directory", file=sys.stderr)
        sys.exit(1)

    ids = sorted({m for m in re.findall(r'tvg-id="([^"]*)"', text) if m})
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write("# Channel ids tracked for the merged EPG.\n")
        fh.write("# Regenerate locally with update_channel_ids.py whenever playlist.m3u changes.\n")
        for i in ids:
            fh.write(i + "\n")
    print(f"wrote {len(ids)} channel ids to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
