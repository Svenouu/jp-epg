"""Translate programme titles/descriptions of an XMLTV tree via Google Translate.

Uses Google's free, key-less web endpoint (translate.googleapis.com, client=gtx).
It is unofficial and can rate-limit or block, so everything here is best effort:
any text that can't be translated is left in Japanese and retried next build.

Translations are cached in TRANSLATION_CACHE (restored/saved between runs by
the workflow's actions/cache step), so each build only sends programmes it
hasn't seen before. The cache is pruned to the texts used by the current
build, so it never grows past one guide's worth.
"""
import copy
import json
import re
import sys
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

TRANSLATION_CACHE = "translation-cache.json"
ENDPOINT = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl={lang}&dt=t"

# One request carries many texts joined by newlines (Google keeps the line
# structure). Kept well under the endpoint's request size limit.
CHUNK_CHARS = 4000
REQUEST_PAUSE = 0.5          # seconds between requests, to stay polite

# Japanese EPG flags like [字] (subtitled) or [デ] (data broadcast). Google
# translates them literally ("[character]"), so the meaningful ones are
# replaced with localized tags and the rest are dropped before translating.
FLAG_RE = re.compile(r"\s*[\[［]([^\]］]{1,2})[\]］]")
FLAG_LABELS = {
    "新": {"en": "[New]", "fr": "[Nouveau]"},
    "終": {"en": "[Final]", "fr": "[Final]"},
    "再": {"en": "[Rerun]", "fr": "[Rediffusion]"},
}


def parse_start(value):
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S %z")
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)


def split_flags(text):
    """Return (text without flags, list of kept flag keys)."""
    kept = [m for m in FLAG_RE.findall(text) if m in FLAG_LABELS]
    return FLAG_RE.sub("", text).strip(), kept


def clean(text):
    # Newlines are the batch separator, so they can't appear inside a text.
    return " ".join((text or "").split())


def google_translate_lines(lines, lang):
    """Translate a list of single-line texts in one request. Returns a list of
    the same length, or None if the request failed or the shape came back
    wrong (so the caller can't mis-assign translations)."""
    body = urllib.parse.urlencode({"q": "\n".join(lines)}).encode()
    req = urllib.request.Request(
        ENDPOINT.format(lang=lang), data=body,
        headers={"User-Agent": "Mozilla/5.0",
                 "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    out = "".join(seg[0] for seg in data[0] if seg and seg[0]).split("\n")
    if len(out) != len(lines):
        return None
    return [s.strip() for s in out]


def chunks(texts):
    batch, size = [], 0
    for t in texts:
        if batch and size + len(t) + 1 > CHUNK_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t) + 1
    if batch:
        yield batch


class Blocked(Exception):
    """Google is refusing this IP (429, or a redirect to its captcha page,
    which urllib surfaces as 405). Retrying in this build is pointless."""


def request_batch(batch, lang):
    """Translate one batch, retrying transient errors. Returns a list aligned
    with batch (None where a text failed), or raises Blocked."""
    for attempt in range(3):
        try:
            result = google_translate_lines(batch, lang)
            if result is None:
                # Line count mismatch: fall back to one text per request.
                result = []
                for t in batch:
                    r = google_translate_lines([t], lang)
                    result.append(r[0] if r else None)
                    time.sleep(REQUEST_PAUSE)
            return result
        except urllib.error.HTTPError as e:
            if e.code in (403, 405, 429):
                raise Blocked(f"HTTP {e.code}") from e
            err = e
        except Exception as e:  # noqa: BLE001 - unofficial endpoint
            err = e
        print(f"  WARN [{lang}]: request failed ({err}); retrying", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    return [None] * len(batch)


def translate_all(ordered_texts, langs, cache):
    """Translate texts missing from the cache, soonest-airing first, sending
    each chunk to every language before moving on — so if Google blocks us
    midway, the next days are covered in all languages rather than one."""
    tables = {lang: cache.setdefault(lang, {}) for lang in langs}
    todo = [t for t in ordered_texts if any(t not in tables[l] for l in langs)]
    print(f"  {len(ordered_texts)} distinct texts, {len(todo)} needing translation "
          f"({sum(map(len, todo))} chars per language at most)")
    sent = dict.fromkeys(langs, 0)
    try:
        for batch in chunks(todo):
            for lang in langs:
                need = [t for t in batch if t not in tables[lang]]
                if not need:
                    continue
                for src, dst in zip(need, request_batch(need, lang)):
                    if dst:
                        tables[lang][src] = dst
                        sent[lang] += 1
                time.sleep(REQUEST_PAUSE)
    except Blocked as e:
        print(f"  WARN: Google Translate is blocking this machine ({e}); the rest "
              f"stays Japanese until a later build", file=sys.stderr)
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
    """Return {lang: translated copy of tree}. Never raises on network trouble:
    untranslated texts just stay in Japanese."""
    # Distinct texts ordered by when they first air, so the nearest days get
    # translated first if Google cuts us off.
    first_air = {}
    for pr in tree.findall("programme"):
        start = parse_start(pr.get("start"))
        for tag in ("title", "desc"):
            el = pr.find(tag)
            if el is not None and (el.text or "").strip():
                text = split_flags(clean(el.text))[0]
                if text and (text not in first_air or start < first_air[text]):
                    first_air[text] = start
    ordered = sorted(first_air, key=first_air.get)

    cache = load_cache()
    translate_all(ordered, langs, cache)
    save_cache(cache, first_air)

    result = {}
    for lang in langs:
        table = cache.get(lang, {})
        out = copy.deepcopy(tree)
        missing = 0
        for pr in out.findall("programme"):
            for tag in ("title", "desc"):
                el = pr.find(tag)
                if el is None or not (el.text or "").strip():
                    continue
                body, flags = split_flags(clean(el.text))
                tr = table.get(body)
                if tr is None:
                    missing += 1
                    continue
                labels = " ".join(FLAG_LABELS[f][lang] for f in flags)
                el.text = f"{labels} {tr}".strip() if tag == "title" else tr
                el.set("lang", lang)
        print(f"  [{lang}] {missing} titles/descriptions left in Japanese")
        result[lang] = out
    return result
