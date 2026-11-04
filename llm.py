"""Localize series titles (and guess missing genres) with an LLM on Azure AI Foundry.

Machine translation renders titles word by word, without context: a known
anime or drama gets a literal title instead of the one it's sold under
("名探偵コナン" -> "Detective Conan" / "Détective Conan" rather than
"Famous Detective Conan"). An LLM given the channel and a bit of the
description recognizes the work and uses its official localized title, and
otherwise translates the title concisely, keeping names in romaji. It also
gives the title's romaji reading, far better than character-by-character
transliteration ("Ohayō Nippon", not "o wa you Nihon").

Needs AZURE_AI_ENDPOINT, AZURE_AI_KEY (and optionally AZURE_AI_DEPLOYMENT,
default "gpt-5.4-mini"). Without them, titles simply go through Azure
Translator like everything else.

Results are cached per Japanese series title in cache["titles"]:
  {"en": ..., "fr": ..., "ro": <Hepburn romaji>, "cat": <enrich.CATEGORY_LABELS key or None>}
or {"none": true} when the model can't/won't handle a title (e.g. a content
filter hit) so it isn't retried every build. Best effort throughout.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from enrich import CATEGORY_LABELS

BATCH = 40          # titles per request
WORKERS = 6         # parallel requests
# Titles sent per build, soonest-airing first; the rest wait for the next one
# (and use Azure Translator meanwhile). A full first run is ~10k titles.
MAX_TITLES_PER_RUN = int(os.environ.get("LLM_MAX_TITLES", "12000"))

SYSTEM = f"""You localize Japanese TV guide (EPG) entries for viewers who don't read Japanese.

For each programme you get its Japanese title, the channel, and the start of its
description. Return, for each one:
- "en": the English title, "fr": the French title, as they should appear in a TV guide.
  If the programme is a known work (anime, drama, film, foreign series, documentary
  series, long-running show...) with an established official English / French title,
  use that title. Otherwise translate the title naturally and concisely. Keep person,
  place and brand names in Hepburn romaji (or their usual Latin spelling). Never add
  information that isn't in the title. Keep series/episode numbers already in the title.
- "ro": the Japanese title written in Hepburn romaji with macrons, as read aloud
  (e.g. "NHKニュースおはよう日本" -> "NHK Nyūsu Ohayō Nippon"); words already in Latin
  letters stay as they are.
- "cat": the best genre among {sorted(CATEGORY_LABELS)}, or null if none fits.

Answer with JSON only: {{"items": [{{"i": <index>, "en": "...", "fr": "...", "ro": "...", "cat": "..."}}, ...]}}
with one item per input, same "i"."""


class ContentFiltered(Exception):
    pass


def config():
    endpoint = os.environ.get("AZURE_AI_ENDPOINT", "").strip()
    key = os.environ.get("AZURE_AI_KEY", "").strip()
    deployment = os.environ.get("AZURE_AI_DEPLOYMENT", "").strip() or "gpt-5.4-mini"
    if not endpoint or not key:
        return None
    return endpoint.rstrip("/") + "/openai/v1/chat/completions", key, deployment


def call(cfg, items):
    url, key, deployment = cfg
    body = json.dumps({
        "model": deployment,
        "reasoning_effort": "low",
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
    }).encode()
    headers = {"api-key": key, "Content-Type": "application/json"}
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
            choice = data["choices"][0]
            if choice.get("finish_reason") == "content_filter":
                raise ContentFiltered()
            return json.loads(choice["message"]["content"]).get("items", [])
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 400 and "content_filter" in detail:
                raise ContentFiltered() from e
            if e.code in (400, 401, 403, 404):
                raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e
            wait = int(e.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
        except (ContentFiltered, RuntimeError):
            raise
        except Exception:  # noqa: BLE001 - network/JSON hiccups: retry
            wait = 10 * (attempt + 1)
        time.sleep(wait)
    return None


def valid(text):
    return isinstance(text, str) and 0 < len(text.strip()) <= 200


def localize_batch(cfg, batch):
    """batch: [(title, channel, hint)]. Returns {title: entry}; recursively
    splits a batch the content filter rejects, down to single titles."""
    items = [{"i": i, "title": t, "channel": ch, "description": hint}
             for i, (t, ch, hint) in enumerate(batch)]
    try:
        answer = call(cfg, items)
    except ContentFiltered:
        if len(batch) == 1:
            return {batch[0][0]: {"none": True}}
        mid = len(batch) // 2
        out = localize_batch(cfg, batch[:mid])
        out.update(localize_batch(cfg, batch[mid:]))
        return out
    if answer is None:
        return {}
    out = {}
    for item in answer:
        try:
            title = batch[int(item["i"])][0]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if valid(item.get("en")) and valid(item.get("fr")):
            cat = item.get("cat")
            out[title] = {"en": item["en"].strip(), "fr": item["fr"].strip(),
                          "ro": item["ro"].strip() if valid(item.get("ro")) else None,
                          "cat": cat if cat in CATEGORY_LABELS else None}
    return out


def localize_titles(wanted, table):
    """wanted: [(title, channel_name, description_hint)], soonest-airing first.
    Fills `table` (cache["titles"]) in place for titles not in it yet."""
    cfg = config()
    todo = [w for w in wanted if w[0] not in table][:MAX_TITLES_PER_RUN]
    print(f"  LLM titles: {len(wanted)} series titles, {len(todo)} new")
    if not todo:
        return
    if cfg is None:
        print("  WARN: AZURE_AI_ENDPOINT / AZURE_AI_KEY not set; titles go through "
              "Azure Translator instead", file=sys.stderr)
        return
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    done = 0
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            for result in pool.map(lambda b: localize_batch(cfg, b), batches):
                table.update(result)
                done += len(result)
    except RuntimeError as e:
        print(f"  WARN: LLM request refused ({e}); remaining titles go through "
              f"Azure Translator this build", file=sys.stderr)
    print(f"  LLM titles: localized {done} titles")
