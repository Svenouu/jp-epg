"""Localize an XMLTV tree: English/French editions and their variants.

Sources, cheapest/best first:
- series titles: an LLM on Azure AI Foundry (llm.py), which knows official
  localized titles of anime, dramas, films...; falls back to Azure Translator;
- episode titles and descriptions: Azure AI Translator, with glossary.json
  forcing the translation of recurring names;
- empty descriptions of films/series: TMDB synopses in each language (tmdb.py);
- romaji of titles: the LLM's reading, else Azure Translator's
  (character-by-character, rougher) transliteration.

Per language three editions are produced: "<lang>" (translated titles),
"<lang>-ja" ("Translated (原題)") and "<lang>-romaji" ("Translated (Romaji)").
The Japanese tree passed in also gets the language-neutral extras in place
(LLM genres where enrich.py found none, TMDB posters/year/rating/synopsis).

Needs AZURE_TRANSLATOR_KEY / AZURE_TRANSLATOR_REGION for Translator (see also
llm.py and tmdb.py for theirs). Everything is best effort: whatever can't be
localized stays in Japanese and is retried next build; nothing here can stop
the build. Everything is cached between runs (cache_store.py), so a build
only pays for programmes it hasn't seen before — upcoming ones first.
"""
import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from xml.sax.saxutils import quoteattr
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import cache_store
import llm
import tmdb
from enrich import CATEGORY_LABELS, reorder

TRANSLATOR = "https://api.cognitive.microsofttranslator.com"
GLOSSARY_FILE = "glossary.json"

# Azure accepts up to 1000 texts / 50,000 characters per request; stay well under.
CHUNK_CHARS = 10000
CHUNK_TEXTS = 500

# Spend guard: Azure bills every source character once per target language
# (and once more for romaji). This caps source characters per language per
# run, so it only ever bites if something goes wrong (e.g. a lost cache).
MAX_SOURCE_CHARS_PER_RUN = int(os.environ.get("TRANSLATE_MAX_CHARS", "600000"))

# Japanese EPG flags like [字] (subtitled). enrich.py already turns title flags
# into XMLTV elements; what's left is mostly the "[終]" / "[生]" prefix it puts
# on descriptions, rendered here as localized labels.
FLAG_RE = re.compile(r"\s*[\[［]([^\]］]{1,2})[\]］]")
FLAG_LABELS = {
    "新": {"en": "[New]", "fr": "[Nouveau]"},
    "終": {"en": "[Final]", "fr": "[Final]"},
    "再": {"en": "[Rerun]", "fr": "[Rediffusion]"},
    "生": {"en": "[Live]", "fr": "[En direct]"},
}

CATEGORY_KEY_BY_JA = {labels["ja"]: key for key, labels in CATEGORY_LABELS.items()}
TMDB_CATEGORIES = {"movie", "drama", "anime", "kids", "documentary"}


# ------------------------------------------------------------------ text --

def clean(text):
    return " ".join((text or "").split())


def split_flags(text):
    """Return (text without flags, list of kept flag keys)."""
    kept = [m for m in FLAG_RE.findall(text) if m in FLAG_LABELS]
    return FLAG_RE.sub("", text).strip(), kept


def tidy_title(text):
    """Post-edit a machine-translated title: stray punctuation/quotes at the
    edges ("En disant que c'est de l'amour,"), doubled spaces, lowercase start."""
    text = clean(text).strip(" ,;:、。・\"“”「」『』")
    if text[:1].islower():
        text = text[0].upper() + text[1:]
    return text


def same(a, b):
    return re.sub(r"\W+", "", (a or "").lower()) == re.sub(r"\W+", "", (b or "").lower())


def parse_start(value):
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S %z")
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)


def priority(start, now):
    """Upcoming programmes soonest first, then past ones most recent first."""
    return (0, start - now) if start >= now else (1, now - start)


# -------------------------------------------------------------- glossary --

def load_glossary():
    """glossary.json: {"日本語": "Same in every language"} or
    {"日本語": {"en": "...", "fr": "..."}}. Keys starting with "_" are comments."""
    try:
        with open(GLOSSARY_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"  WARN: {GLOSSARY_FILE} unreadable ({e}); no glossary", file=sys.stderr)
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and v}


def glossary_for(glossary, lang):
    out = {}
    for ja, val in glossary.items():
        tr = val.get(lang) if isinstance(val, dict) else val
        if tr:
            out[ja] = tr
    return out


def glossary_markup(text, terms, pattern):
    """Wrap glossary terms in Azure's dynamic-dictionary markup."""
    if pattern is None:
        return text
    return pattern.sub(lambda m: f"<mstrans:dictionary translation={quoteattr(terms[m.group(0)])}>"
                                 f"{m.group(0)}</mstrans:dictionary>", text)


def invalidate_changed_glossary(cache, glossary, langs):
    """Drop cached translations of texts containing a glossary term that was
    added or changed since the last build, so the new entry takes effect."""
    previous = cache.get("glossary", {})
    changed = [k for k, v in glossary.items() if previous.get(k) != v]
    if changed:
        dropped = 0
        for lang in langs:
            table = cache.get(lang, {})
            for text in [t for t in table if any(k in t for k in changed)]:
                del table[text]
                dropped += 1
        print(f"  glossary: {len(changed)} new/changed terms, {dropped} cached "
              f"translations redone")
    cache["glossary"] = glossary


# ------------------------------------------------------------ translator --

class Stop(Exception):
    """Translator can't continue this build (bad key, quota exhausted...)."""


def azure(path, texts, key, region):
    """POST texts to a Translator endpoint; returns the parsed JSON list or
    None after retries."""
    body = json.dumps([{"Text": t} for t in texts]).encode()
    headers = {"Ocp-Apim-Subscription-Key": key,
               "Ocp-Apim-Subscription-Region": region,
               "Content-Type": "application/json; charset=UTF-8"}
    for attempt in range(5):
        try:
            req = urllib.request.Request(TRANSLATOR + path, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            if len(data) != len(texts):
                raise ValueError(f"got {len(data)} results for {len(texts)} texts")
            return data
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (400, 401, 403):
                raise Stop(f"HTTP {e.code}: {detail}") from e
            wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
            print(f"  WARN: Azure HTTP {e.code}, retrying in {wait}s ({detail})",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - network is allowed to flake
            wait = 10 * (attempt + 1)
            print(f"  WARN: Azure request failed ({e}), retrying in {wait}s", file=sys.stderr)
        time.sleep(wait)
    return None


def chunks(texts):
    batch, size = [], 0
    for t in texts:
        if batch and (size + len(t) > CHUNK_CHARS or len(batch) >= CHUNK_TEXTS):
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t)
    if batch:
        yield batch


def run_jobs(label, ordered, table, send):
    """Send texts missing from `table`, in order, through `send(batch)` ->
    list of results aligned with batch (None = failed). Stops at the budget."""
    todo = [t for t in ordered if t not in table]
    budget, done = MAX_SOURCE_CHARS_PER_RUN, 0
    for batch in chunks(todo):
        cost = sum(map(len, batch))
        if cost > budget:
            print(f"  WARN: [{label}] per-run limit of {MAX_SOURCE_CHARS_PER_RUN} source "
                  f"chars reached; the rest waits for the next build", file=sys.stderr)
            break
        budget -= cost
        for src, dst in zip(batch, send(batch)):
            if dst:
                table[src] = dst
                done += 1
    print(f"  [{label}] {len(todo)} new texts, {done} done "
          f"({MAX_SOURCE_CHARS_PER_RUN - budget} source chars billed)")


def translate_texts(ordered, langs, cache, glossary):
    key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
    region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
    if not key or not region:
        print("  WARN: AZURE_TRANSLATOR_KEY / AZURE_TRANSLATOR_REGION not set; "
              "skipping translation and romaji", file=sys.stderr)
        return
    try:
        for lang in langs:
            terms = glossary_for(glossary, lang)
            pattern = (re.compile("|".join(map(re.escape, sorted(terms, key=len, reverse=True))))
                       if terms else None)
            table = cache.setdefault(lang, {})
            for ja, tr in terms.items():          # whole-text glossary hits: free
                if ja in ordered:
                    table.setdefault(ja, tr)

            def send(batch, lang=lang, terms=terms, pattern=pattern):
                data = azure(f"/translate?api-version=3.0&from=ja&to={lang}",
                             [glossary_markup(t, terms, pattern) for t in batch], key, region)
                if data is None:
                    return [None] * len(batch)
                return [(item["translations"][0]["text"] or "").strip() or None for item in data]

            run_jobs(lang, ordered, table, send)
    except Stop as e:
        print(f"  WARN: Azure Translator refused the request ({e}); the rest stays "
              f"Japanese until a later build", file=sys.stderr)


def romanize(titles, cache):
    key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
    region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
    if not key or not region:
        return
    table = cache.setdefault("romaji", {})

    def send(batch):
        data = azure("/transliterate?api-version=3.0&language=ja&fromScript=Jpan&toScript=Latn",
                     batch, key, region)
        if data is None:
            return [None] * len(batch)
        return [tidy_title(item.get("text") or "") or None for item in data]

    try:
        run_jobs("romaji", titles, table, send)
    except Stop as e:
        print(f"  WARN: Azure transliteration refused ({e})", file=sys.stderr)


# ------------------------------------------------------------- metadata --

def add_tmdb(pr, info):
    """Add TMDB's language-neutral extras to a Japanese programme."""
    if info.get("poster") and pr.find("icon") is None:
        ET.SubElement(pr, "icon", {"src": info["poster"]})
    if info.get("year") and info.get("type") == "movie" and pr.find("date") is None:
        ET.SubElement(pr, "date").text = info["year"]
    if info.get("rating") and pr.find("star-rating") is None:
        ET.SubElement(ET.SubElement(pr, "star-rating", {"system": "TMDB"}),
                      "value").text = f"{info['rating']}/10"


# -------------------------------------------------------------- editions --

def translated_trees(tree, langs):
    now = datetime.now(timezone.utc)
    channel_names = {c.get("id"): c.findtext("display-name") or c.get("id")
                     for c in tree.findall("channel")}
    programmes = tree.findall("programme")

    title_rank, text_rank, context, title_cat = {}, {}, {}, {}
    for pr in programmes:
        key = priority(parse_start(pr.get("start")), now)
        title = clean(pr.findtext("title"))
        desc = clean(pr.findtext("desc"))
        cat = CATEGORY_KEY_BY_JA.get(clean(pr.findtext("category")))
        if title:
            if title not in title_rank or key < title_rank[title]:
                title_rank[title] = key
            if title not in context or (desc and not context[title][1]):
                context[title] = (channel_names.get(pr.get("channel"), ""), desc[:120])
            title_cat.setdefault(title, cat)
        for tag in ("sub-title", "desc"):
            text = split_flags(clean(pr.findtext(tag)))[0]
            if text and (text not in text_rank or key < text_rank[text]):
                text_rank[text] = key
    titles = sorted(title_rank, key=title_rank.get)

    cache = cache_store.load()
    glossary = load_glossary()
    invalidate_changed_glossary(cache, glossary, langs)

    # 1. Series titles through the LLM (adult titles skipped: content filters).
    llm_titles = cache.setdefault("titles", {})
    llm.localize_titles([(t, *context[t]) for t in titles if title_cat.get(t) != "adult"],
                        llm_titles)

    def llm_ok(t):
        entry = llm_titles.get(t) or {}
        return all(entry.get(lang) for lang in langs)

    # 2. Everything else (and titles the LLM didn't cover) through Translator.
    for t in titles:
        if not llm_ok(t) and (t not in text_rank or title_rank[t] < text_rank[t]):
            text_rank[t] = title_rank[t]
    translate_texts(sorted(text_rank, key=text_rank.get), langs, cache, glossary)
    romanize([t for t in titles if not (llm_titles.get(t) or {}).get("ro")], cache)

    # 3. Genres the LLM found where enrich.py had none; TMDB for films/series.
    for t in titles:
        if title_cat.get(t) is None and (llm_titles.get(t) or {}).get("cat"):
            title_cat[t] = llm_titles[t]["cat"]
    tmdb_table = cache.setdefault("tmdb", {})
    tmdb.lookup([t for t in titles if title_cat.get(t) in TMDB_CATEGORIES],
                tmdb_table, llm_titles)

    # Keep only what this guide uses, so the cache never outgrows one guide.
    used = set(text_rank) | set(title_rank)
    for name in (*langs, "romaji"):
        cache[name] = {k: v for k, v in cache.get(name, {}).items() if k in used}
    for name in ("titles", "tmdb"):
        cache[name] = {k: v for k, v in cache.get(name, {}).items() if k in title_rank}
    cache_store.save(cache)

    # Language-neutral extras, applied to the Japanese tree itself.
    tmdb_desc = {}          # programme index -> TMDB info, for empty descriptions
    added_cats = 0
    for i, pr in enumerate(programmes):
        title = clean(pr.findtext("title"))
        if pr.find("category") is None and title_cat.get(title):
            ET.SubElement(pr, "category", {"lang": "ja"}).text = \
                CATEGORY_LABELS[title_cat[title]]["ja"]
            added_cats += 1
        info = tmdb_table.get(title)
        if info:
            add_tmdb(pr, info)
            desc = pr.find("desc")
            if desc is None or not clean(desc.text):
                tmdb_desc[i] = info
                if info.get("ja"):
                    if desc is None:
                        desc = ET.SubElement(pr, "desc")
                    desc.text = info["ja"]
                    desc.set("lang", "ja")
        reorder(pr)
    print(f"  metadata: {added_cats} genres from the LLM, "
          f"{sum(1 for p in programmes if tmdb_table.get(clean(p.findtext('title'))))} "
          f"programmes matched on TMDB, {len(tmdb_desc)} empty descriptions filled from it")

    romaji = cache.get("romaji", {})
    result = {}
    for lang in langs:
        table = cache.get(lang, {})
        variants = {name: copy.deepcopy(tree) for name in (lang, f"{lang}-ja", f"{lang}-romaji")}
        progs = {name: t.findall("programme") for name, t in variants.items()}
        missing = 0
        for i, pr in enumerate(programmes):
            ja_title = clean(pr.findtext("title"))
            entry = llm_titles.get(ja_title) or {}
            tr_title = entry.get(lang) or table.get(ja_title)
            tr_title = tidy_title(tr_title) if tr_title else None
            ro = entry.get("ro") or romaji.get(ja_title)
            for name, plist in progs.items():
                p = plist[i]
                el = p.find("title")
                if el is not None and ja_title:
                    if tr_title is None:
                        missing += name == lang
                        if name.endswith("-romaji") and ro:
                            el.text = ro
                    else:
                        el.set("lang", lang)
                        if name.endswith("-ja") and not same(tr_title, ja_title):
                            el.text = f"{tr_title} ({ja_title})"
                        elif name.endswith("-romaji") and ro and not same(tr_title, ro):
                            el.text = f"{tr_title} ({ro})"
                        else:
                            el.text = tr_title
                for tag in ("sub-title", "desc"):
                    el = p.find(tag)
                    if tag == "desc" and i in tmdb_desc and tmdb_desc[i].get(lang):
                        if el is None:
                            el = ET.SubElement(p, "desc")
                            reorder(p)
                        el.text = tmdb_desc[i][lang]
                        el.set("lang", lang)
                        continue
                    if el is None or not clean(el.text):
                        continue
                    body, flags = split_flags(clean(el.text))
                    labels = " ".join(FLAG_LABELS[f][lang] for f in flags)
                    tr = table.get(body) if body else ""
                    if tr is None:
                        missing += name == lang
                        continue
                    if tag == "sub-title" and tr:
                        tr = tidy_title(tr)
                    el.text = f"{labels} {tr}".strip()
                    el.set("lang", lang)
                for cat in p.findall("category"):
                    key = CATEGORY_KEY_BY_JA.get(clean(cat.text))
                    if key:
                        cat.text = CATEGORY_LABELS[key][lang]
                        cat.set("lang", lang)
        print(f"  [{lang}] {missing} titles/sub-titles/descriptions left in Japanese")
        result.update(variants)
    return result
