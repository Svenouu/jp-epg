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
2. Fetches the upstream EPG; if that's down, falls back to a second public
   EPG source that uses completely different (Japanese-named) channel ids,
   remapped via FALLBACK_CHANNEL_MAP. The fallback only covers the major
   networks plus a chunk of BS/CS channels (85 of our 100), never the full
   lineup, so it uses its own lower channel-count threshold.
3. Keeps only channels in channel_ids.txt, trims to fit a size ceiling,
   reformats one-element-per-line.
4. When the primary source served the run, fills its empty programme
   descriptions from the fallback source (best effort: matched by channel and
   absolute start time; skipped silently if the fallback is unreachable).
5. Refuses to publish a near-empty or decoy-looking result (safety nets).
6. Structures the flat Japanese titles (episode numbers, flags, categories,
   posters) — see enrich.py.
7. Keeps CATCHUP_DAYS of past programmes so the player's replay/catch-up
   has listings to browse.
8. Writes English and French editions (epg-en.xml, epg-fr.xml) with titles
   and descriptions machine-translated — see translate.py — plus bilingual
   variants (epg-en-ja.xml, epg-fr-ja.xml) whose titles also carry the
   original. Every edition is also written gzipped (.xml.gz).
"""
import sys
import urllib.request
import gzip
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

from enrich import enrich, strip_posters
from translate import translated_trees

# Translated editions published next to epg-merged.xml as epg-<lang>.xml
# (and epg-<lang>-ja.xml for the bilingual-title variant).
TRANSLATED_LANGS = ("en", "fr")

# Past days kept in the guide. The playlist offers catchup-days="6"; players
# only let you replay what the EPG still lists, so keep a day more than that.
CATCHUP_DAYS = 7

# Keep the published file comfortably under a size that's proven to load on
# constrained media-player XML parsers.
SIZE_CAP = int(19.5 * 1024 * 1024)

# Upstream EPG source, read from the playlist's own url-tvg header. Contains
# no per-account tokens (unlike the playlist itself), so it's safe to commit.
EPG_SRC = "https://0i4neux01.charandom.blog/epg/kai-epg.xml"

# Used only if EPG_SRC is unreachable. A public, independently-run EPG with
# no per-account tokens either, but its channel ids are display-name-based
# Japanese strings with no relation to ours — see FALLBACK_CHANNEL_MAP.
EPG_FALLBACK_SRC = "https://epgshare01.online/epgshare01/epg_ripper_JP2.xml.gz"

# our tvg-id -> EPG_FALLBACK_SRC's channel id. Hand-built by matching channel
# names (verified against a live fetch: all 85 ids existed and had >=1
# programme). Only covers the major networks and some BS/CS channels, not
# the many numbered BS/CS aliases EPG_SRC provides directly — if the
# fallback renames one of these, that single channel just silently drops
# out of coverage (harmless unless many do, which MIN_CHANNELS_FALLBACK
# below catches).
FALLBACK_CHANNEL_MAP = {
    "cs15": "BBCニュース.jp",
    "bs_11": "BS11.イレブン.jp",
    "bs_12": "BS12トゥエルビ.jp",
    "bs_yoshimoto": "BSよしもと.jp",
    "bs05": "BSテレ東.jp",
    "cs16": "CNNｊ.jp",
    "dlife": "Dlife（ディーライフ）.jp",
    "fighting_tv_samurai": "FIGHTING.TV.サムライ.jp",
    "cs17": "GAORA.SPORTS.HD.jp",
    "bs18": "J.SPORTS.1.HD.jp",
    "bs19": "J.SPORTS.2.HD.jp",
    "bs21": "J.SPORTS.3.HD.jp",
    "bs22": "J.SPORTS.4.HD.jp",
    "bs31": "J:COM.BS.jp",
    "kbs-world_jp": "KBS.WORLD.HD.jp",
    "kntv": "KNTV.jp",
    "cs19": "LaLa.TV.jp",
    "cs21": "MONDOTV.HD.jp",
    "cs18": "MTV.HD.jp",
    "mnet_jp": "Mnet.HD.jp",
    "gd02": "NHK東京　教育.jp",
    "gd01": "NHK東京　総合.jp",
    "gd04": "TBS.jp",
    "tbs_channel_1": "TBSチャンネル1.最新ドラマ・音楽・映画.jp",
    "tbs_channel_2": "TBSチャンネル2.名作ドラマ・スポーツ・アニメ.jp",
    "gd08": "TOKYO　MX.jp",
    "v_paradise_nsfw": "V☆パラダイス.jp",
    "wowow_plus": "WOWOWプラス.映画・ドラマ・スポーツ・音楽.jp",
    "action_channel": "アクションチャンネル.jp",
    "bs15": "アニマックスＨＤ.jp",
    "cs24": "アニマルプラネット.jp",
    "at-x": "アニメシアターX（AT-X）.jp",
    "cs25": "カートゥーン.ネットワークHD.海外アニメ国内アニメ.jp",
    "cs07": "キッズステーション.HD.jp",
    "bs14": "グリーンチャンネルHD.jp",
    "cs03": "ゴルフネットワーク.jp",
    "the_cinema": "ザ・シネマ.HD.jp",
    "cs01": "スカイA.jp",
    "sport_live_plus": "スポーツライブ＋.jp",
    "cs28": "タカラヅカ・スカイ・ステージ.jp",
    "cs29": "チャンネル銀河　歴史ドラマ・サスペンス・日本のうた.jp",
    "gd06": "テレビ朝日.jp",
    "tv_asahi_channel_1": "テレ朝チャンネル１.jp",
    "tv_asahi_channel_2": "テレ朝チャンネル２.jp",
    "gd07": "テレ東.jp",
    "cs08": "ディスカバリーチャンネル.jp",
    "cs23": "ディズニージュニア.jp",
    "bs24": "ディズニー・チャンネル.jp",
    "cs10": "ナショナル.ジオグラフィック.jp",
    "cs09": "ヒストリーチャンネル.日本・世界の歴史＆エンタメ.jp",
    "cs20": "ファミリー劇場HD.jp",
    "gd05": "フジテレビ.jp",
    "cs26": "フジテレビＮＥＸＴ　ライブ・プレミアム.jp",
    "fuji_tv_one": "フジテレビＯＮＥ　スポーツ・バラエティ.jp",
    "fuji_tv_two": "フジテレビＴＷＯ　ドラマ・アニメ.jp",
    "cs05": "ホームドラマチャンネルＨＤ.韓流・時代劇・国内ドラマ.jp",
    "mystery_channel": "ミステリーチャンネル.jp",
    "movie_plus": "ムービープラス.jp",
    "me-tele_next": "メ〜テレNEXT.ドラマ・エンタメ・ダンス.jp",
    "shogi_channel": "囲碁・将棋チャンネル.jp",
    "cs12": "旅チャンネル　ＨＤ.jp",
    "ntv_news24": "日テレNEWS24.HD.jp",
    "cs02": "日テレジータス.HD.jp",
    "nittele_plus": "日テレプラス.ドラマ・アニメ・音楽ライブ.jp",
    "gd03": "日本テレビ.jp",
    "bs23": "日本映画専門チャンネル　HD.jp",
    "neco_ch": "映画･チャンネルNECO-HD.jp",
    "cs04": "時代劇専門チャンネル.HD.jp",
    "cs27": "東映チャンネルHD.jp",
    "cs13": "歌謡ポップスチャンネル.jp",
    "cs22": "衛星劇場HD.jp",
    "bs25": "釣りビジョンHD.jp",
    "bs26": "音楽・ライブ！　スペースシャワーTV.HD.jp",
    "bs06": "ＢＳフジ・181.jp",
    "bs02": "ＢＳ日テレ.jp",
    "bs03": "ＢＳ朝日１.jp",
    "bs04": "ＢＳ－ＴＢＳ.jp",
    "bs10": "ＢＳ１０.jp",
    "bs08": "ＢＳ１０プレミアム.jp",
    "bs11": "ＮＨＫ　ＢＳ.jp",
    "bs01": "ＮＨＫ　ＢＳＰ４Ｋ.jp",
    "cs11": "ＴＢＳ.ＮＥＷＳ.jp",
    "bs07": "ＷＯＷＯＷシネマ.jp",
    "bs12": "ＷＯＷＯＷプライム.jp",
    "bs20": "ＷＯＷＯＷライブ.jp",
}

CHANNEL_IDS_FILE = "channel_ids.txt"

# Max start-time gap (seconds) for two sources' programmes to count as the
# same slot when borrowing descriptions. Starts normally match exactly; a few
# primary-source channels carry odd seconds offsets (e.g. 00:30:06).
DESC_MATCH_TOLERANCE = 180

# Safety nets (see main()): abort rather than publish a broken/decoy feed.
MIN_CHANNELS = 70            # primary source: last observed match 98/100
MIN_CHANNELS_FALLBACK = 50   # fallback source: last observed match 85/100
MAX_TITLE_SHARE = 0.5        # one title must not dominate like this


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


def remap_fallback_ids(root):
    """Rewrite the fallback feed's channel ids to our own tvg-ids in place,
    using FALLBACK_CHANNEL_MAP, so downstream filtering doesn't need to know
    which source produced the data. Elements with no entry in the map are
    left untouched and simply won't match `needed` later."""
    inverse = {fid: our_id for our_id, fid in FALLBACK_CHANNEL_MAP.items()}
    for ch in root.findall("channel"):
        our_id = inverse.get(ch.get("id"))
        if our_id:
            ch.set("id", our_id)
    for pr in root.findall("programme"):
        our_id = inverse.get(pr.get("channel"))
        if our_id:
            pr.set("channel", our_id)


def load_source():
    """Fetch the primary EPG; fall back to the secondary source (remapped to
    our channel ids) if the primary is unreachable. Returns (root, used_fallback)."""
    root = repaired_root(fetch(EPG_SRC))
    if root is not None:
        return root, False

    print("  WARN: primary EPG source unavailable; trying fallback", file=sys.stderr)
    root = repaired_root(fetch(EPG_FALLBACK_SRC))
    if root is None:
        return None, True
    remap_fallback_ids(root)
    return root, True


def parse_xmltv_time(value):
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S %z")
    except (TypeError, ValueError):
        return None


def fill_missing_descs(out):
    """Fill empty <desc> of primary-source programmes from the fallback source.

    The primary feed leaves descriptions empty for a whole set of channels
    (WOWOW, the_cinema, TBS/TV Asahi channels...), while epgshare01 carries
    them. Programmes are matched per channel on absolute start time (the two
    feeds use different UTC offsets). Best effort: a fallback outage just
    leaves descriptions as they were."""
    root = repaired_root(fetch(EPG_FALLBACK_SRC, tries=2))
    if root is None:
        print("  WARN: fallback unavailable; descriptions not enriched", file=sys.stderr)
        return
    remap_fallback_ids(root)

    by_channel = {}
    for pr in root.findall("programme"):
        desc = (pr.findtext("desc") or "").strip()
        start = parse_xmltv_time(pr.get("start"))
        if desc and start:
            by_channel.setdefault(pr.get("channel"), []).append((start, desc))

    filled = missing = 0
    for pr in out.findall("programme"):
        el = pr.find("desc")
        if el is not None and (el.text or "").strip():
            continue
        missing += 1
        candidates = by_channel.get(pr.get("channel"))
        start = parse_xmltv_time(pr.get("start"))
        if not candidates or start is None:
            continue
        gap, desc = min(((abs((s - start).total_seconds()), d) for s, d in candidates),
                        key=lambda x: x[0])
        if gap > DESC_MATCH_TOLERANCE:
            continue
        if el is None:
            el = ET.Element("desc")
            title = pr.find("title")
            pr.insert(list(pr).index(title) + 1 if title is not None else 0, el)
        el.text = desc
        el.set("lang", "ja")
        filled += 1
    print(f"descriptions: filled {filled} of {missing} empty from fallback source")


def load_channel_ids():
    with open(CHANNEL_IDS_FILE, encoding="utf-8") as fh:
        return {
            line.strip()
            for line in fh
            if line.strip() and not line.startswith("#")
        }


def trim_to_fit(merged):
    """Trim the merged guide so it stays under SIZE_CAP: keeps CATCHUP_DAYS of
    history, and as many forward days as fit. Programme posters are kept
    unless dropping them is what it takes to fit the full forward window."""
    channels = [e for e in merged if e.tag == "channel"]
    programmes = [e for e in merged if e.tag == "programme"]

    jst = datetime.now(timezone.utc) + timedelta(hours=9)  # Japanese broadcast day
    lo = (jst - timedelta(days=CATCHUP_DAYS)).strftime("%Y%m%d")

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

    def size_of(tv):
        return len(ET.tostring(tv, encoding="utf-8"))

    tv, kept = build((jst + timedelta(days=8)).strftime("%Y%m%d"))
    size = size_of(tv)
    if size <= SIZE_CAP:
        print(f"  window {lo}..+8d with posters: {kept} programmes, {size/1e6:.1f} MB (fits)")
        return tv
    print(f"  window {lo}..+8d with posters: {size/1e6:.1f} MB over cap, dropping posters")
    strip_posters(merged)

    # Take the largest forward window that fits; self-adapts to daily density.
    for fwd in range(8, 1, -1):
        hi = (jst + timedelta(days=fwd)).strftime("%Y%m%d")
        tv, kept = build(hi)
        size = size_of(tv)
        if size <= SIZE_CAP:
            print(f"  window {lo}..+{fwd}d: {kept} programmes, {size/1e6:.1f} MB (fits)")
            return tv
        print(f"  window {lo}..+{fwd}d: {size/1e6:.1f} MB over cap, trying shorter")
    tv, kept = build((jst + timedelta(days=1)).strftime("%Y%m%d"))
    print(f"  fallback {lo}..+1d: {kept} programmes")
    return tv


def write_xmltv(tree, path):
    """Serialize one element per line (ElementTree's default output is a single
    multi-megabyte line, which some player-side XML parsers reject), to `path`
    and gzipped to `path`.gz."""
    attrs = "".join(f' {k}="{v}"' for k, v in tree.attrib.items())
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n', f"<tv{attrs}>\n"]
    for el in tree:
        parts.append(ET.tostring(el, encoding="unicode"))
        parts.append("\n")
    parts.append("</tv>\n")
    data = "".join(parts).encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    with open(path + ".gz", "wb") as f:
        f.write(gzip.compress(data, compresslevel=9, mtime=0))
    print(f"WROTE {path}: {len(tree.findall('channel'))} channels, "
          f"{len(tree.findall('programme'))} programmes, {len(data)/1e6:.1f} MB")


def main():
    needed = load_channel_ids()
    print(f"tracking {len(needed)} channel ids from {CHANNEL_IDS_FILE}")

    root, used_fallback = load_source()
    if root is None:
        print("ERROR: primary and fallback EPG sources both unavailable — "
              "refusing to overwrite the live EPG", file=sys.stderr)
        sys.exit(1)
    print(f"source: {'fallback' if used_fallback else 'primary'}")

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

    # Safety net: abort rather than publish a feed that's missing most of its
    # channels — upstream likely changed channel ids or is serving a broken
    # file. The fallback only ever covers a subset (major networks + some
    # BS/CS), so it gets its own, lower threshold.
    min_required = MIN_CHANNELS_FALLBACK if used_fallback else MIN_CHANNELS
    if n_ch < min_required:
        print(f"ERROR: only {n_ch} channels (< {min_required}) — upstream EPG or "
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

    if not used_fallback:
        fill_missing_descs(out)

    enrich(out)
    final = trim_to_fit(out)

    write_xmltv(final, "epg-merged.xml")

    # English/French editions: same guide, titles and descriptions translated,
    # plus bilingual-title variants (keyed "<lang>-ja"). Best effort — a
    # translation outage leaves text in Japanese but never blocks publishing
    # the main file above.
    try:
        editions = translated_trees(final, TRANSLATED_LANGS)
    except Exception as e:  # noqa: BLE001 - must not cost us the main EPG
        print(f"  WARN: translation failed ({e}); publishing untranslated copies",
              file=sys.stderr)
        editions = {name: final for lang in TRANSLATED_LANGS
                    for name in (lang, f"{lang}-ja")}
    for name, tree in editions.items():
        write_xmltv(tree, f"epg-{name}.xml")

if __name__ == "__main__":
    main()
