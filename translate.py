"""Translate programme titles/descriptions of an XMLTV tree with Azure AI Translator.

Needs two environment variables (GitHub Actions secrets in the workflow):
  AZURE_TRANSLATOR_KEY     key of an Azure "Translator" resource
  AZURE_TRANSLATOR_REGION  that resource's region (e.g. "francecentral")
Without them, translation is skipped and the translated editions stay in
Japanese. (Google's free web endpoint was tried first: it rejects GitHub
Actions runners outright with HTTP 429.)

Everything here is best effort: any text that can't be translated is left in
Japanese and retried next build; nothing here can stop the build.

Translations are cached in TRANSLATION_CACHE (restored/saved between runs by
the workflow's actions/cache step), so each build only pays for programmes it
hasn't seen before. The cache is pruned to the texts used by the current
build, so it never grows past one guide's worth.
"""
import copy
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request

from enrich import CATEGORY_LABELS

TRANSLATION_CACHE = "translation-cache.json"
ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&from=ja"

# Azure accepts up to 1000 texts / 50,000 characters per request; stay well
# under so one request (sent for both languages at once) stays small.
CHUNK_CHARS = 10000
CHUNK_TEXTS = 500

# Spend guard: Azure bills every source character once per target language
# (S1 tier: ~$10 per million). A full guide is ~480k source characters, a
# normal day's new programmes far less, so this only ever bites if something
# goes wrong (e.g. the cache got lost every run). Override with
# TRANSLATE_MAX_CHARS.
MAX_SOURCE_CHARS_PER_RUN = int(os.environ.get("TRANSLATE_MAX_CHARS", "600000"))

# Japanese EPG flags like [字] (subtitled) or [デ] (data broadcast). Machine
# translation renders them literally ("[character]"), so the meaningful ones
# are replaced with localized tags and the rest are dropped before translating.
# enrich.py already turns title flags into XMLTV elements; what's left here is
# mostly the "[終]" / "[生]" prefix it puts on descriptions.
FLAG_RE = re.compile(r"\s*[\[［]([^\]］]{1,2})[\]］]")
FLAG_LABELS = {
    "新": {"en": "[New]", "fr": "[Nouveau]"},
    "終": {"en": "[Final]", "fr": "[Final]"},
    "再": {"en": "[Rerun]", "fr": "[Rediffusion]"},
    "生": {"en": "[Live]", "fr": "[En direct]"},
}

TRANSLATED_TAGS = ("title", "sub-title", "desc")

# Japanese category label -> {lang: label}; fixed table, no API call needed.
CATEGORY_BY_JA = {labels["ja"]: labels for labels in CATEGORY_LABELS.values()}


def parse_start(value):
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S %z")
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)


def priority(start, now):
    """Sort key: upcoming programmes soonest first, then past ones most recent
    first — so if translation stops midway, what's about to air is covered
    before the catch-up history."""
    if start >= now:
        return (0, start - now)
    return (1, now - start)


def split_flags(text):
    """Return (text without flags, list of kept flag keys)."""
    kept = [m for m in FLAG_RE.findall(text) if m in FLAG_LABELS]
    return FLAG_RE.sub("", text).strip(), kept


def clean(text):
    return " ".join((text or "").split())


class Stop(Exception):
    """Translation can't continue this build (bad key, quota exhausted...)."""


def azure_translate(texts, langs, key, region):
    """Translate texts into every lang in one request.
    Returns {lang: [translation, ...]} aligned with texts."""
    url = ENDPOINT + "".join(f"&to={lang}" for lang in langs)
    body = json.dumps([{"Text": t} for t in texts]).encode()
    headers = {"Ocp-Apim-Subscription-Key": key,
               "Ocp-Apim-Subscription-Region": region,
               "Content-Type": "application/json; charset=UTF-8"}
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            if len(data) != len(texts):
                raise ValueError(f"got {len(data)} results for {len(texts)} texts")
            out = {lang: [] for lang in langs}
            for item in data:
                by_lang = {t["to"]: t["text"] for t in item["translations"]}
                for lang in langs:
                    out[lang].append((by_lang.get(lang) or "").strip() or None)
            return out
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (400, 401, 403):
                # Bad key/region, or free-tier quota used up: retrying won't help.
                raise Stop(f"HTTP {e.code}: {detail}") from e
            wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
            print(f"  WARN: Azure HTTP {e.code}, retrying in {wait}s ({detail})",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - network is allowed to flake
            wait = 10 * (attempt + 1)
            print(f"  WARN: Azure request failed ({e}), retrying in {wait}s",
                  file=sys.stderr)
        time.sleep(wait)
    return {lang: [None] * len(texts) for lang in langs}


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


def translate_all(ordered_texts, langs, cache):
    """Translate texts missing from the cache, soonest-airing first, so if
    translation stops midway the next days are the ones covered."""
    key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
    region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
    tables = {lang: cache.setdefault(lang, {}) for lang in langs}
    todo = [t for t in ordered_texts if any(t not in tables[l] for l in langs)]
    print(f"  {len(ordered_texts)} distinct texts, {len(todo)} needing translation "
          f"({sum(map(len, todo))} source chars)")
    if not todo:
        return
    if not key or not region:
        print("  WARN: AZURE_TRANSLATOR_KEY / AZURE_TRANSLATOR_REGION not set; "
              "skipping translation", file=sys.stderr)
        return

    sent = dict.fromkeys(langs, 0)
    budget = MAX_SOURCE_CHARS_PER_RUN
    try:
        for batch in chunks(todo):
            need = [l for l in langs if any(t not in tables[l] for t in batch)]
            cost = sum(map(len, batch))
            if cost > budget:
                print(f"  WARN: per-run limit of {MAX_SOURCE_CHARS_PER_RUN} source "
                      f"chars reached; the rest waits for the next build", file=sys.stderr)
                break
            budget -= cost
            result = azure_translate(batch, need, key, region)
            for lang in need:
                for src, dst in zip(batch, result[lang]):
                    if dst:
                        tables[lang][src] = dst
                        sent[lang] += 1
    except Stop as e:
        print(f"  WARN: Azure Translator refused the request ({e}); the rest stays "
              f"Japanese until a later build", file=sys.stderr)
    print(f"  sent {MAX_SOURCE_CHARS_PER_RUN - budget} source chars to Azure")
    for lang in langs:
        print(f"  [{lang}] translated {sent[lang]} new texts")


def load_cache():
    try:
        with open(TRANSLATION_CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache, used):
    pruned = {lang: {t: tr for t, tr in table.items() if t in used}
              for lang, table in cache.items()}
    with open(TRANSLATION_CACHE, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, ensure_ascii=False, separators=(",", ":"))


def translated_trees(tree, langs):
    """Return {lang: translated copy of tree, f"{lang}-ja": same with
    "Translated (原題)" titles}. Never raises on network trouble: untranslated
    texts just stay in Japanese."""
    # Distinct texts ordered by when they air relative to now, so the coming
    # days get translated first if translation stops midway.
    now = datetime.now(timezone.utc)
    rank = {}
    for pr in tree.findall("programme"):
        key = priority(parse_start(pr.get("start")), now)
        for tag in TRANSLATED_TAGS:
            el = pr.find(tag)
            if el is not None and (el.text or "").strip():
                text = split_flags(clean(el.text))[0]
                if text and (text not in rank or key < rank[text]):
                    rank[text] = key
    ordered = sorted(rank, key=rank.get)

    cache = load_cache()
    translate_all(ordered, langs, cache)
    save_cache(cache, rank)

    result = {}
    for lang in langs:
        table = cache.get(lang, {})
        out = copy.deepcopy(tree)
        bilingual = copy.deepcopy(tree)
        missing = 0
        for pr, bi in zip(out.findall("programme"), bilingual.findall("programme")):
            for tag in TRANSLATED_TAGS:
                el = pr.find(tag)
                if el is None or not (el.text or "").strip():
                    continue
                body, flags = split_flags(clean(el.text))
                labels = " ".join(FLAG_LABELS[f][lang] for f in flags)
                tr = table.get(body) if body else ""
                if tr is None:
                    missing += 1
                    continue
                text = f"{labels} {tr}".strip()
                bi_el = bi.find(tag)
                if tag == "title" and tr and tr != body:
                    bi_el.text = f"{text} ({body})"
                else:
                    bi_el.text = text
                el.text = text
                el.set("lang", lang)
                bi_el.set("lang", lang)
            for p in (pr, bi):
                for cat in p.findall("category"):
                    labels = CATEGORY_BY_JA.get((cat.text or "").strip())
                    if labels:
                        cat.text = labels[lang]
                        cat.set("lang", lang)
        print(f"  [{lang}] {missing} titles/sub-titles/descriptions left in Japanese")
        result[lang] = out
        result[f"{lang}-ja"] = bilingual
    return result
