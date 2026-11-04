"""Film/series metadata from TMDB (themoviedb.org): synopsis, poster, year, rating.

The upstream guide has no description for thousands of programmes, mostly on
film and drama channels, which is exactly what TMDB covers well — with
synopses already written in French, English and Japanese, so they cost no
translation.

Needs TMDB_READ_TOKEN (TMDB account -> Settings -> API -> "API Read Access
Token"). Without it this module does nothing.

Matching is deliberately strict, since a wrong match is worse than none: a
search result is only accepted when its title equals (after normalization)
the programme's Japanese title, a work quoted inside it (「...」), or the
official English title the LLM gave (llm.py) — and its year, when the title
carries one ("黄金(1948)"). Results (including "no match") are cached per Japanese title
in cache["tmdb"].
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import stats

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
MAX_LOOKUPS_PER_RUN = int(os.environ.get("TMDB_MAX_LOOKUPS", "1500"))
MIN_VOTES = 20      # below this a TMDB rating says little
LANGS = {"ja": "ja-JP", "en": "en-US", "fr": "fr-FR"}


def norm(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\W_]+", "", text)


def get(path, token, **params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise
            if e.code == 404:
                return None
            time.sleep(int(e.headers.get("Retry-After") or 0) or 2 * (attempt + 1))
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return None


YEAR_RE = re.compile(r"\s*[(（]((?:19|20)\d\d)[)）]\s*$")
QUOTED_RE = re.compile(r"[『「]([^『』「」]{2,})[』」]")


def queries(title, en):
    """(query, language, year) candidates, most specific first: the title,
    the title without a trailing "(1948)" (then the year must match), a work
    quoted inside a longer title (「...」/『...』), the LLM's English title."""
    out = []
    for text, lang in ((title, "ja"), (en, "en")):
        if not text:
            continue
        m = YEAR_RE.search(text)
        if m:
            out.append((text[:m.start()], lang, m.group(1)))
        else:
            out.append((text, lang, None))
        if lang == "ja":
            out.extend((q, "ja", None) for q in QUOTED_RE.findall(text))
    return out


def search(token, query, lang, year=None):
    data = get("/search/multi", token, query=query, language=LANGS[lang], include_adult="false")
    want = norm(query)
    for res in (data or {}).get("results", [])[:10]:
        kind = res.get("media_type")
        if kind not in ("movie", "tv"):
            continue
        if year and (res.get("release_date") or res.get("first_air_date") or "")[:4] != year:
            continue
        names = (res.get("title"), res.get("name"), res.get("original_title"),
                 res.get("original_name"))
        if want and any(norm(n) == want for n in names if n):
            return kind, res["id"]
    return None


def details(token, kind, tmdb_id):
    info = {"type": kind, "id": tmdb_id}
    for lang, code in LANGS.items():
        data = get(f"/{kind}/{tmdb_id}", token, language=code)
        if not data:
            continue
        if data.get("overview"):
            info[lang] = data["overview"].strip()
        if lang == "ja":
            if data.get("poster_path"):
                info["poster"] = IMG + data["poster_path"]
            year = (data.get("release_date") or data.get("first_air_date") or "")[:4]
            if year.isdigit():
                info["year"] = year
            if (data.get("vote_count") or 0) >= MIN_VOTES and data.get("vote_average"):
                info["rating"] = round(float(data["vote_average"]), 1)
    return info


def lookup(wanted, table, llm_titles):
    """wanted: Japanese series titles (soonest-airing first). Fills `table`
    (cache["tmdb"]) in place: {title: info} or {title: {}} for no match."""
    token = os.environ.get("TMDB_READ_TOKEN", "").strip()
    todo = [t for t in wanted if t not in table][:MAX_LOOKUPS_PER_RUN]
    print(f"  TMDB: {len(wanted)} film/series titles, {len(todo)} new")
    stats.record("tmdb_candidates", len(wanted))
    stats.record("tmdb_pending", max(0, len([t for t in wanted if t not in table]) - len(todo)))
    if not todo:
        return
    if not token:
        print("  WARN: TMDB_READ_TOKEN not set; skipping TMDB", file=sys.stderr)
        return
    found = 0
    try:
        for title in todo:
            hit = None
            for query, lang, year in queries(title, (llm_titles.get(title) or {}).get("en")):
                hit = search(token, query, lang, year)
                if hit:
                    break
            table[title] = details(token, *hit) if hit else {}
            found += bool(hit)
    except urllib.error.HTTPError as e:
        print(f"  WARN: TMDB refused the token (HTTP {e.code}); skipping TMDB",
              file=sys.stderr)
    print(f"  TMDB: matched {found} of {len(todo)} looked up")
    stats.record("tmdb_looked_up", len(todo))
    stats.record("tmdb_matched_new", found)
