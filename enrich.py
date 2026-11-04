"""Turn the upstream feed's flat Japanese titles into structured XMLTV data.

The upstream guide packs everything into <title>: broadcast flags
("[新]", "(二)", "【無料】"...), episode numbers ("#12", "第3話",
"シーズン2"), episode titles ("「牧之介のお願いの段」") and episode counts
("(全27話)"). Players such as TiviMate can't use any of that, and it breaks
series recordings (every episode looks like a different show) and the
translation cache (every episode is a new text to pay for).

`enrich(root)` rewrites every <programme> in place:

- flags become XMLTV elements: <new/> (TiviMate's NEW badge),
  <previously-shown/>, <last-chance/>, <subtitles/>, bilingual <audio>;
  final-episode / live flags also show up as a "[終]" / "[生]" prefix on the
  description, which translate.py localizes ("[Final]", "[En direct]"...).
- episode/season numbers move to <episode-num> (xmltv_ns + onscreen), and the
  text after them to <sub-title>, leaving a clean, stable series title;
- a <category> is derived from title keywords, else from the channel;
- channel-logo images that upstream attaches as programme <icon>s are dropped
  (real posters are kept), and children are put back in XMLTV DTD order.
"""
import re
import unicodedata
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- flags --

# Bracketed tokens that are broadcast flags, not part of the show's name.
# The feed writes the long-vowel mark "ー" as "-", hence a few doubles.
FLAG_GROUPS = {
    "new": {"新", "初", "新番組"},
    "rerun": {"再", "再放送"},
    "final": {"終", "最終回"},
    "live": {"生", "生放送", "生中継"},
    "subtitles": {"字", "字幕", "字幕版", "CC", "SS"},
    "bilingual": {"二", "ニ", "多", "二カ国語", "二ヵ国語", "二か国語"},
    "dubbed": {"吹", "吹替", "吹替版", "吹き替え"},
    "drop": {"無料", "無", "無料放送", "デ", "解", "手", "S", "双", "HV", "N", "B",
             "SD", "HD", "5.1", "PPV", "KNTV", "韓"},
}
FLAG_OF = {tok: group for group, toks in FLAG_GROUPS.items() for tok in toks}
FLAG_RE = re.compile(r"[\[(<【〔]\s*([^\[\]()<>【】〔〕]{1,5}?)\s*[\])>】〕]")
# Same information written without brackets at the end of kids' titles:
# "二字-日英" = bilingual audio + subtitles (Japanese/English).
TAIL_FLAG_RE = re.compile(r"\s*二字(?:\s*[-/]\s*日英?)?\s*$")

# Visible prefixes kept on the description (translate.py localizes them).
DESC_PREFIX = {"final": "[終]", "live": "[生]"}

# -------------------------------------------------------------- episodes --

SEASON_RE = re.compile(r"(?:シーズン|シ-ズン|Season|SEASON|season)\s*(\d{1,2})|(?<![A-Za-z])S(\d{1,2})(?!\d)")
EPISODE_RE = re.compile(r"#\s*(\d{1,4})(?:\s*[-~〜]\s*(\d{1,4}))?|第\s*(\d{1,4})\s*話")
TOTAL_RE = re.compile(r"\(\s*全\s*(\d{1,4})\s*話\s*\)")
QUOTED_TAIL_RE = re.compile(r"^(.+?)\s*「([^「」]+)」$")
EDGE_JUNK = " \t-:/・◆▽▼◇★☆|,、。"

# ------------------------------------------------------------ categories --

# Label per category key, per edition language. The Japanese label is what
# epg-merged.xml carries; translate.py swaps it for the edition's language.
CATEGORY_LABELS = {
    "news":        {"ja": "ニュース",       "en": "News",        "fr": "Actualités"},
    "movie":       {"ja": "映画",           "en": "Movie",       "fr": "Film"},
    "drama":       {"ja": "ドラマ",         "en": "Series",      "fr": "Série"},
    "sports":      {"ja": "スポーツ",       "en": "Sports",      "fr": "Sport"},
    "anime":       {"ja": "アニメ",         "en": "Animation",   "fr": "Animation"},
    "kids":        {"ja": "キッズ",         "en": "Kids",        "fr": "Jeunesse"},
    "music":       {"ja": "音楽",           "en": "Music",       "fr": "Musique"},
    "documentary": {"ja": "ドキュメンタリー", "en": "Documentary", "fr": "Documentaire"},
    "shopping":    {"ja": "ショッピング",    "en": "Shopping",    "fr": "Téléachat"},
    "adult":       {"ja": "アダルト",        "en": "Adult",       "fr": "Adulte"},
}

# Title keywords, checked in this order (first hit wins). Matched against the
# title with "-" turned back into "ー".
KEYWORD_CATEGORIES = [
    ("shopping", ("ショッピング", "通販", "買物", "ショップ", "テレショップ")),
    ("news", ("ニュース", "NEWS", "News", "報道", "ヘッドライン", "天気")),
    ("anime", ("アニメ",)),
    ("sports", ("野球", "サッカー", "ゴルフ", "競馬", "テニス", "バレーボール", "ラグビー",
                "相撲", "プロレス", "ボクシング", "格闘", "Jリーグ", "駅伝", "マラソン",
                "スケート", "卓球", "バドミントン", "バスケ", "MLB", "NBA", "F1", "競輪",
                "競艇", "ボートレース", "陸上", "柔道", "UFC", "WWE")),
    ("movie", ("映画", "洋画", "邦画", "ロードショー", "シネマ")),
    ("documentary", ("ドキュメンタリー", "ドキュメント")),
    ("drama", ("ドラマ", "時代劇", "連続テレビ小説")),
    ("kids", ("キッズ", "こども", "子ども")),
    ("music", ("音楽", "ミュージック", "コンサート", "歌謡", "MUSIC")),
]

# Fallback when no keyword matches: what the channel mostly airs.
# General-interest networks (NHK, NTV, TBS, BS...) deliberately have none.
CHANNEL_CATEGORIES = {
    "news": ("ntv_news24", "cs11", "cs15", "cs16"),
    "movie": ("bs07", "the_cinema", "neco_ch", "bs23", "cs27", "movie_plus", "cs22"),
    "drama": ("cs05", "cs04", "mystery_channel", "cs29", "cs20", "kntv", "kbs-world_jp",
              "dlife", "tbs_channel_1", "tbs_channel_2", "tv_asahi_channel_1",
              "cs19", "action_channel", "me-tele_next"),
    "sports": ("bs18", "bs19", "bs21", "bs22", "cs17", "cs03", "cs01", "cs02",
               "sport_live_plus", "fighting_tv_samurai", "bs14", "bs25"),
    "anime": ("at-x", "bs15", "cs07", "cs25"),
    "kids": ("bs24", "cs23"),
    "music": ("cs06", "music_on_tv", "cs18", "mnet_jp", "bs26", "cs13"),
    "documentary": ("cs10", "cs08", "cs24", "cs09", "tetsu", "cs12"),
    "adult": ("pigoo_nsfw", "v_paradise_nsfw"),
}
CHANNEL_CATEGORY = {ch: cat for cat, chs in CHANNEL_CATEGORIES.items() for ch in chs}

# XMLTV DTD child order for <programme>; some parsers are strict about it.
DTD_ORDER = ["title", "sub-title", "desc", "credits", "date", "category", "keyword",
             "language", "orig-language", "length", "icon", "url", "country",
             "episode-num", "video", "audio", "previously-shown", "premiere",
             "last-chance", "new", "subtitles", "rating", "star-rating", "review"]


def strip_flags(title):
    """Remove known bracketed flags. Returns (title, set of flag groups)."""
    found = set()

    def repl(m):
        group = FLAG_OF.get(m.group(1).strip())
        if group is None:
            return m.group(0)            # e.g. 【新宿編】: part of the name
        found.add(group)
        return " "

    title = FLAG_RE.sub(repl, title)
    if TAIL_FLAG_RE.search(title):
        found.update(("bilingual", "subtitles"))
        title = TAIL_FLAG_RE.sub("", title)
    return " ".join(title.split()), found


def tidy(text):
    return text.strip(EDGE_JUNK).strip()


def split_episode(title):
    """Pull season/episode/total and the episode's own title out of a title.
    Returns (series_title, sub_title, season, episode, episode_end, total)."""
    total = None
    m = TOTAL_RE.search(title)
    if m:
        total = int(m.group(1))
        title = " ".join((title[:m.start()] + " " + title[m.end():]).split())

    sm, em = SEASON_RE.search(title), EPISODE_RE.search(title)
    marks = [x for x in (sm, em) if x]
    if not marks:
        q = QUOTED_TAIL_RE.match(title)       # 番組名「その回のタイトル」
        if q and tidy(q.group(1)):
            return tidy(q.group(1)), q.group(2).strip(), None, None, None, total
        return title, None, None, None, None, total

    first = min(x.start() for x in marks)
    head = tidy(title[:first])
    if not head:                               # title starts with "#3": leave it
        return title, None, None, None, None, total

    season = int(sm.group(1) or sm.group(2)) if sm else None
    episode = episode_end = None
    if em:
        episode = int(em.group(1) or em.group(3))
        episode_end = int(em.group(2)) if em.group(2) else None

    rest = title[first:]
    rest = SEASON_RE.sub(" ", rest, count=1) if sm else rest
    rest = EPISODE_RE.sub(" ", rest, count=1) if em else rest
    rest = tidy(" ".join(rest.split()))
    q = re.fullmatch(r"[「【]([^「」【】]+)[」】]", rest)
    sub = (q.group(1) if q else rest) or None
    return head, sub, season, episode, episode_end, total


def episode_nums(season, episode, episode_end, total):
    """<episode-num> values: xmltv_ns (zero-based, used for S/E display) and
    a human-readable onscreen form."""
    out = []
    if episode is not None and episode_end is None:
        s = str(season - 1) if season else ""
        e = str(episode - 1) + (f"/{total}" if total else "")
        out.append(("xmltv_ns", f"{s}.{e}."))
    elif season and episode is None:
        out.append(("xmltv_ns", f"{season - 1}.."))
    parts = []
    if season:
        parts.append(f"S{season}")
    if episode is not None:
        parts.append(f"E{episode}" + (f"-{episode_end}" if episode_end else ""))
    if parts:
        out.append(("onscreen", " ".join(parts)))
    return out


def category_for(title, channel):
    if channel in CHANNEL_CATEGORIES["adult"]:
        return "adult"
    probe = title.replace("-", "ー")
    for cat, words in KEYWORD_CATEGORIES:
        if any(w in probe or w in title for w in words):
            return cat
    return CHANNEL_CATEGORY.get(channel)


def is_channel_logo(icon):
    return "/channel/icon/" in (icon.get("src") or "")


def enrich_programme(pr):
    title_el = pr.find("title")
    raw = (title_el.text or "") if title_el is not None else ""
    title, flags = strip_flags(unicodedata.normalize("NFKC", raw))
    series, sub, season, episode, episode_end, total = split_episode(title)
    if not series:                    # a title that was nothing but flags
        series = title or raw
    if "dubbed" in flags:             # dubbed and subtitled airings differ
        series += " (吹替)"
    if title_el is not None:
        title_el.text = series

    # Tags we (re)generate are cleared first, so enrichment is idempotent.
    for tag in ("sub-title", "category", "episode-num", "audio", "previously-shown",
                "premiere", "last-chance", "new", "subtitles"):
        for el in pr.findall(tag):
            pr.remove(el)

    if sub and sub != series:
        ET.SubElement(pr, "sub-title", {"lang": "ja"}).text = sub

    prefix = " ".join(DESC_PREFIX[f] for f in ("final", "live") if f in flags)
    if prefix:
        desc = pr.find("desc")
        if desc is None:
            desc = ET.SubElement(pr, "desc", {"lang": "ja"})
        body = (desc.text or "").strip()
        desc.text = f"{prefix} {body}".strip()

    cat = category_for(series, pr.get("channel"))
    if cat:
        ET.SubElement(pr, "category", {"lang": "ja"}).text = CATEGORY_LABELS[cat]["ja"]

    for system, value in episode_nums(season, episode, episode_end, total):
        ET.SubElement(pr, "episode-num", {"system": system}).text = value

    if "bilingual" in flags:
        ET.SubElement(ET.SubElement(pr, "audio"), "stereo").text = "bilingual"
    if "rerun" in flags:
        ET.SubElement(pr, "previously-shown")
    if "final" in flags:
        ET.SubElement(pr, "last-chance")
    if "new" in flags:
        ET.SubElement(pr, "new")
    if "subtitles" in flags:
        ET.SubElement(pr, "subtitles", {"type": "onscreen"})

    icons = [ic for ic in pr.findall("icon")]
    for ic in icons:
        pr.remove(ic)
    posters = [ic for ic in icons if not is_channel_logo(ic)]
    children = list(pr) + posters[:1]
    for el in list(pr):
        pr.remove(el)
    rank = {tag: i for i, tag in enumerate(DTD_ORDER)}
    children.sort(key=lambda el: rank.get(el.tag, len(DTD_ORDER)))
    pr.extend(children)
    return flags, episode is not None or season is not None, cat


def enrich(root):
    n = with_flags = with_ep = with_cat = 0
    for pr in root.findall("programme"):
        flags, has_ep, cat = enrich_programme(pr)
        n += 1
        with_flags += bool(flags)
        with_ep += has_ep
        with_cat += bool(cat)
    print(f"enriched {n} programmes: {with_ep} with episode numbers, "
          f"{with_flags} with flags, {with_cat} with a category")


def strip_posters(root):
    for pr in root.findall("programme"):
        for ic in pr.findall("icon"):
            pr.remove(ic)
