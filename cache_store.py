"""Load/save the build's cache (translations, LLM titles, romaji, TMDB matches).

The cache lives in a local JSON file, which the workflow also keeps in the
GitHub Actions cache. When AZURE_CACHE_SAS_URL is set (a container URL with a
SAS token, e.g. https://acct.blob.core.windows.net/epg-cache?sv=...), the file
is also synced to Azure Blob Storage: downloaded before the build, uploaded
after. Actions caches get evicted (unused for 7 days, or past the repo's
quota); losing this one would mean paying to retranslate the whole guide, so
Blob is the durable copy and the Actions cache only a fallback.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CACHE_FILE = "translation-cache.json"
BLOB_NAME = "translation-cache.json"


def blob_url():
    base = os.environ.get("AZURE_CACHE_SAS_URL", "").strip()
    if not base:
        return None
    parts = urllib.parse.urlsplit(base)
    path = parts.path.rstrip("/") + "/" + BLOB_NAME
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def load():
    url = blob_url()
    if url:
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data = r.read()
            json.loads(data)                       # don't clobber with garbage
            with open(CACHE_FILE, "wb") as fh:
                fh.write(data)
            print(f"cache: restored {len(data)/1e6:.1f} MB from Azure Blob")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print(f"  WARN: cache download failed (HTTP {e.code}); using local copy",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - a cache miss must never stop the build
            print(f"  WARN: cache download failed ({e}); using local copy", file=sys.stderr)
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            cache = json.load(fh)
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def save(cache):
    data = json.dumps(cache, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(CACHE_FILE, "wb") as fh:
        fh.write(data)
    url = blob_url()
    if not url:
        return
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=300):
            pass
        print(f"cache: saved {len(data)/1e6:.1f} MB to Azure Blob")
    except Exception as e:  # noqa: BLE001
        print(f"  WARN: cache upload failed ({e}); kept locally only", file=sys.stderr)
