# jp-epg

A personal, auto-refreshing TV guide (XMLTV/EPG) for my own Japanese IPTV
playlist — built for players that load only one EPG URL.

This is a fork of the idea behind
[skinred78/jp-iptv-epg](https://github.com/skinred78/jp-iptv-epg), adapted to
a different upstream playlist/EPG pair and rebuilt so the playlist itself
(which embeds per-account stream auth tokens) never touches this repo.

## The one URL

```
EPG source:  https://svenouu.github.io/jp-epg/epg-merged.xml
```

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
3. Either way, keeps only the channels in `channel_ids.txt`, trims the
   result to fit under a size ceiling, and writes `epg-merged.xml`.
4. `.github/workflows/build-epg.yml` runs that script twice daily (16:00 &
   04:00 JST) and force-pushes the result to the `dist` branch, which GitHub
   Pages serves. The daily commit also keeps the scheduled workflow from
   being auto-disabled for inactivity.

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
