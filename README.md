# jp-epg

A personal, auto-refreshing TV guide (XMLTV/EPG) for my own Japanese IPTV
playlist — built for players that load only one EPG URL.

This is a fork of the idea behind
[skinred78/jp-iptv-epg](https://github.com/skinred78/jp-iptv-epg), adapted to
a different upstream playlist/EPG pair and rebuilt so the playlist itself
(which embeds per-account stream auth tokens) never touches this repo.

## The URLs

```
Japanese (original):  https://svenouu.github.io/jp-epg/epg-merged.xml
English:              https://svenouu.github.io/jp-epg/epg-en.xml
French:               https://svenouu.github.io/jp-epg/epg-fr.xml
```

Title variants — `"Translated title (原題)"`, or `"Translated title
(Romaji)"` to read/say/search the original name with a Latin keyboard:

```
English + original:   https://svenouu.github.io/jp-epg/epg-en-ja.xml
French + original:    https://svenouu.github.io/jp-epg/epg-fr-ja.xml
English + romaji:     https://svenouu.github.io/jp-epg/epg-en-romaji.xml
French + romaji:      https://svenouu.github.io/jp-epg/epg-fr-romaji.xml
```

Every file also exists gzipped: append `.gz` (e.g. `epg-fr.xml.gz`, ~2 MB
instead of ~12 MB) — TiviMate and most players read it directly, and it loads
much faster on low-end Android/Fire TV boxes.

All of them cover the same channels and time window: the last 7 days (so the
playlist's 6-day catch-up/replay has listings to browse) and up to 8 days
ahead. The English and French editions have programme titles, episode titles
and descriptions localized (see *Localization* below). Pick the one you want and point your
player at it.

### What the guide carries (beyond title/description)

Upstream packs everything into the Japanese title; `enrich.py` unpacks it:

- **Series title + `<episode-num>`/`<sub-title>`**: `ドラマ #12「別れ」` becomes
  title `ドラマ`, episode `E12` (also `xmltv_ns`, so players show S/E), episode
  title `別れ`. A stable series title is what makes TiviMate's series
  recordings work, and it keeps the translation cache small.
- **Flags → XMLTV elements**: `[新]`/`[初]` → `<new/>` (TiviMate's NEW
  badge), `[再]` → `<previously-shown/>`, `[字]` → `<subtitles/>`, `[二]` →
  bilingual `<audio>`, `[終]` → `<last-chance/>`; final and live
  programmes also get a `[Final]`/`[Live]` (`[終]`/`[生]`) prefix on their
  description. Noise like `【無料】` is dropped.
- **`<category>`**: from title keywords, else from what the channel mostly
  airs (`CHANNEL_CATEGORIES` in `enrich.py`), else the LLM's guess (see
  below). Localized per edition.
- **`ー` restored**: upstream writes the katakana long-vowel mark as `-`
  (`ニュ-ス`), which translation can't read; it's put back.
- **Posters**: programme thumbnails are kept (channel logos that upstream
  attaches as programme icons are dropped — the playlist provides those).
  If the guide ever outgrows the size ceiling, posters go first, then
  forward days.

Served by GitHub Pages from the `dist` branch as `application/xml`, gzip
compressed in transit. Point your player's EPG/XMLTV setting at it once — it
refreshes itself.

The playlist (`playlist.m3u`) stays local — it's in `.gitignore` and is never
committed, since its `catchup-source`/stream URLs carry per-account access
tokens. Only the channel ids it references are committed, in
`channel_ids.txt`, which carries no secrets.

## How it works

1. `channel_ids.txt` lists the `tvg-id`s this EPG should cover (extracted
   from `playlist.m3u` with `update_channel_ids.py`, run locally).
2. `merge_epg.py` fetches the upstream merged EPG (its address is read from
   the playlist's `url-tvg` header — hardcoded in the script since, unlike
   the playlist, it carries no tokens). If that source is unreachable, it
   falls back to a second public EPG (`epgshare01.online`) that uses
   completely different, Japanese-named channel ids — remapped to ours via
   `FALLBACK_CHANNEL_MAP`. The fallback only covers 85 of the 100 tracked
   channels (the major networks plus a chunk of BS/CS), never the full
   lineup.
3. Either way, keeps only the channels in `channel_ids.txt`. When the
   primary source served the run, its empty programme descriptions (it has
   none at all for ~30 channels, e.g. WOWOW, the_cinema, TBS/TV Asahi
   channels) are filled from epgshare01, matched by channel and absolute
   start time. epgshare01 only lists the next few days fully, so later days
   stay mostly description-less.
4. `enrich.py` structures each programme (see above), then the result is
   trimmed to fit under a size ceiling and written as `epg-merged.xml`.
5. `translate.py` then writes `epg-en.xml` and `epg-fr.xml` (plus the `-ja`
   and `-romaji` variants) — see *Localization* below. Everything is cached
   between runs, so a build only pays for programmes it hasn't seen before —
   upcoming ones first (soonest first), then the catch-up history. Anything
   that fails stays in Japanese and is retried at the next build — it never
   blocks publishing.
6. `.github/workflows/build-epg.yml` runs that script twice daily (16:00 &
   04:00 JST) and force-pushes the files (`.xml` and `.xml.gz`) to the
   `dist` branch, which GitHub Pages serves. The daily commit also keeps the scheduled workflow from
   being auto-disabled for inactivity.

## Localization

| What | Source | Module |
|---|---|---|
| Series titles (official EN/FR title when the work is known), romaji, missing genres | LLM `gpt-5.4-mini` on Azure AI Foundry | `llm.py` |
| Episode titles, descriptions, titles the LLM didn't cover | Azure AI Translator + `glossary.json` | `translate.py` |
| Synopsis (FR/EN/JA), poster, year, rating of films & series | TMDB | `tmdb.py` |
| Cache of all of the above | Azure Blob Storage (+ Actions cache as fallback) | `cache_store.py` |

Azure resources, all in resource group `EPG` (West Europe): `EPG-Translator`
(Translator S1), `epg-ai` (AI Services, deployment `gpt-5.4-mini`),
`epgcacheq1wh2z` (Storage, container `epg-cache`).

**Glossary.** `glossary.json` forces how recurring names are translated by
Azure Translator (`"名探偵コナン": {"en": "Detective Conan", "fr": "Détective
Conan"}`, or one string for every language). Editing an entry retranslates
the cached texts containing it at the next build. The LLM doesn't need it for
series titles.

**Repository secrets** (Settings → Secrets and variables → Actions):

| Secret | What |
|---|---|
| `AZURE_TRANSLATOR_KEY` / `AZURE_TRANSLATOR_REGION` | Translator key and region (`westeurope`) |
| `AZURE_AI_ENDPOINT` / `AZURE_AI_KEY` | `epg-ai` endpoint and key |
| `AZURE_CACHE_SAS_URL` | `https://<account>.blob.core.windows.net/epg-cache?<SAS>` (read/create/write, renew before it expires — 2 years) |
| `TMDB_READ_TOKEN` | TMDB account → Settings → API → *API Read Access Token* |

Each one is optional: without it, that step is skipped (titles go through
Translator, the cache lives in the Actions cache only, no TMDB data...).

### Translator setup

The English/French editions need an Azure **Translator** resource (Azure
portal → Create a resource → Translator; tier S1, ~$10 per million
characters, billed per target language — F0's free 2M chars/month is too
small for two languages). Then add two repository secrets (Settings →
Secrets and variables → Actions):

- `AZURE_TRANSLATOR_KEY` — one of the resource's keys (Keys and Endpoint)
- `AZURE_TRANSLATOR_REGION` — its location, e.g. `francecentral`

Secrets are encrypted, never visible in the repo or in logs, and not exposed
to workflows triggered from forks. Without them the build still works; the
translated editions just stay in Japanese.

Cost: a whole guide (7 days back + up to 8 ahead) is ~820k Japanese
characters × 2 languages ≈ $16 to translate from an empty cache; later
builds only pay for new programmes (title variants and categories cost
nothing extra). The LLM costs a few dollars for a full first pass (~10k
series titles) and cents per day after; TMDB is free. A run that hits the cap below just finishes at the next build. `TRANSLATE_MAX_CHARS`
(default 600,000 source characters per run) caps spend if the cache is ever
lost. Google's free web endpoint was tried first and rejects GitHub Actions
runners outright (HTTP 429).

## Safety nets

The build aborts (keeping the last-good published file) instead of
overwriting it with something broken, if:

- fewer than 70 (primary source) or 50 (fallback source) of the tracked
  channel ids come through — upstream likely changed channel ids or is
  serving a broken file.
- one programme title accounts for more than half of all listings — a
  signature of a placeholder/notice feed rather than a real guide.

Check the Action run's log line `source: primary` / `source: fallback` to
see which one actually served a given run.

## Maintaining the fallback mapping

`FALLBACK_CHANNEL_MAP` in `merge_epg.py` is a hand-built, verified-once
correspondence between our `tvg-id`s and epgshare01's channel ids. If
epgshare01 renames or drops one of the 85 mapped channels, that single
channel silently stops being covered when the fallback engages — harmless
unless enough of them do to trip `MIN_CHANNELS_FALLBACK`. There's no
automated way to detect a stale single entry; if the fallback ever actually
engages, it's worth spot-checking a few mapped channels against epgshare01's
current file.

## Maintaining the channel list

Whenever `playlist.m3u`'s channel lineup changes, run locally:

```
python update_channel_ids.py
```

then commit the updated `channel_ids.txt`. This only ever reads/writes
`tvg-id` values — it never touches the playlist's stream URLs.

## Maintenance

- **Run it now:** Actions tab → *Build EPG* → *Run workflow*.
- **Guide looks frozen for more than a day?** Check the latest Action run's
  log for one of the two safety-net `ERROR` lines above — upstream likely
  changed shape and needs a look before the fix ships.
