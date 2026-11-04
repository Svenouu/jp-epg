"""Per-build statistics, published with the EPG as a status page.

Modules call `record()` / `add()` as they go; merge_epg.py then calls
`publish()`, which appends this build to the history (fetched from the
published site, since the dist branch is rebuilt from scratch each run) and
writes stats-history.json and status.html next to the EPG files.

Costs are estimates from Azure retail list prices (USD), which ignore any
credit or discount on the subscription; check Azure Cost Management for the
actual bill.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HISTORY_URL = "https://svenouu.github.io/jp-epg/stats-history.json"
HISTORY_FILE = "stats-history.json"
STATUS_FILE = "status.html"
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_template.html")
KEEP_BUILDS = 120                 # ~2 months at two builds a day

# Azure retail prices, USD (checked 2026-11 via prices.azure.com).
TRANSLATOR_PER_M_CHARS = 10.0     # Translator S1, per target language
LLM_IN_PER_M = 0.75               # gpt-5.4-mini Global Standard, input tokens
LLM_OUT_PER_M = 4.50              # ... output tokens (reasoning included)

STARTED = time.time()
STATS = {}


def record(key, value):
    STATS[key] = value


def add(key, value):
    STATS[key] = STATS.get(key, 0) + value


def costs():
    translator = STATS.get("translator_chars", 0) / 1e6 * TRANSLATOR_PER_M_CHARS
    llm = (STATS.get("llm_tokens_in", 0) / 1e6 * LLM_IN_PER_M
           + STATS.get("llm_tokens_out", 0) / 1e6 * LLM_OUT_PER_M)
    return round(translator, 4), round(llm, 4)


def load_history():
    try:
        with urllib.request.urlopen(HISTORY_URL, timeout=60) as r:
            data = json.loads(r.read())
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001 - first run, or the site is down
        print(f"  WARN: no previous stats history ({e})", file=sys.stderr)
        return []


def publish(ok=True):
    translator, llm = costs()
    entry = dict(STATS,
                 time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 run_id=os.environ.get("GITHUB_RUN_ID"),
                 duration_s=round(time.time() - STARTED),
                 ok=ok,
                 cost_translator=translator, cost_llm=llm,
                 cost_total=round(translator + llm, 4))
    history = [h for h in load_history() if h.get("run_id") != entry["run_id"]]
    history = (history + [entry])[-KEEP_BUILDS:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, separators=(",", ":"))
    try:
        with open(TEMPLATE, encoding="utf-8") as fh:
            page = fh.read()
        data = json.dumps(history, ensure_ascii=False).replace("</", "<\\/")
        with open(STATUS_FILE, "w", encoding="utf-8") as fh:
            fh.write(page.replace("/*__HISTORY__*/[]", data))
    except OSError as e:
        print(f"  WARN: status page not written ({e})", file=sys.stderr)
    print(f"stats: build cost ~${entry['cost_total']:.2f} "
          f"(Translator ${translator:.2f}, LLM ${llm:.2f}); {len(history)} builds in history")
