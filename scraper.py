#!/usr/bin/env python3
"""Daily job board scraper. No AI. Filters by title and location, keeps a ledger.

Outputs (written to the repo, committed by the GitHub Action):
  alerts/latest.md and alerts/YYYY-MM-DD.md   new, still open, closed since last run
  dropped/latest.md                           title matches rejected by location, with reason
  state/seen.json                             ledger: first seen, last seen, closed date
"""
import json, os, re, sys, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
TODAY = dt.date.today().isoformat()
CA = os.environ.get("REQUESTS_CA_BUNDLE") or ("/root/.ccr/ca-bundle.crt" if os.path.exists("/root/.ccr/ca-bundle.crt") else True)
T_IN = re.compile(CFG["title_include"], re.I)
T_OUT = re.compile(CFG["title_exclude"], re.I)
L_TARGET = re.compile(CFG["location_target"], re.I)
L_OPEN = re.compile(CFG["location_open"], re.I)
L_CONFLICT = re.compile(CFG["location_conflict"], re.I)
HEADERS = {"User-Agent": "jobboardmonitor/1.0"}


def get(url):
    r = requests.get(url, timeout=30, headers=HEADERS, verify=CA)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(b):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs")
    return [dict(id=str(j["id"]), title=j["title"], location=(j.get("location") or {}).get("name", ""),
                 url=j["absolute_url"], posted=(j.get("first_published") or j.get("updated_at") or "")[:10])
            for j in d.get("jobs", [])]


def fetch_ashby(b):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{b}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") if isinstance(s, dict) else str(s) for s in j.get("secondaryLocations") or []]
        loc = "; ".join(x for x in locs if x)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc += " (remote)"
        out.append(dict(id=j["id"], title=j["title"], location=loc, url=j.get("jobUrl", ""),
                        posted=(j.get("publishedAt") or "")[:10]))
    return out


def fetch_lever(b):
    d = get(f"https://api.lever.co/v0/postings/{b}?mode=json")
    return [dict(id=j["id"], title=j["text"], location=(j.get("categories") or {}).get("location", "") or "",
                 url=j["hostedUrl"], posted=dt.datetime.fromtimestamp(j["createdAt"] / 1000, dt.timezone.utc).date().isoformat() if j.get("createdAt") else "")
            for j in d]


FETCH = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "lever": fetch_lever}


def run_board(args):
    src, b = args
    try:
        return src, b, FETCH[src](b), None
    except Exception as e:
        return src, b, None, f"{type(e).__name__}: {str(e)[:80]}"


def classify(job):
    """Return (keep, reason). Judge by title first, then location."""
    t, loc = job["title"], job["location"]
    if not T_IN.search(t):
        return None, None  # not a sales style title, ignore silently
    if T_OUT.search(t):
        return False, "title excluded"
    if not loc.strip():
        return True, "no location listed, check job page"
    if L_TARGET.search(loc):
        return True, ""
    if L_OPEN.search(loc) and not L_CONFLICT.search(loc):
        return True, "generic remote or EMEA, confirm Spain on the job page"
    return False, "location: " + loc[:60]


def main():
    ledger_path = os.path.join(ROOT, "state", "seen.json")
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    ledger = json.load(open(ledger_path)) if os.path.exists(ledger_path) else {}
    baseline = not ledger

    tasks = [(s, b) for s in ("greenhouse", "ashby", "lever") for b in CFG[s]]
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(run_board, tasks))

    ok_boards, errors, total = set(), [], 0
    matched, dropped = {}, []
    for src, b, jobs, err in results:
        if err:
            errors.append(f"{src}/{b}: {err}")
            continue
        ok_boards.add(f"{src}:{b}")
        total += len(jobs)
        for j in jobs:
            keep, why = classify(j)
            if keep is None:
                continue
            j.update(company=b, source=src, note=why)
            key = f"{src}:{b}:{j['id']}"
            if keep:
                matched[key] = j
            elif why.startswith("location"):
                dropped.append(j)

    new, still, closed = [], [], []
    for key, j in matched.items():
        if key not in ledger:
            ledger[key] = dict(first_seen=TODAY, last_seen=TODAY, closed=None, **{k: j[k] for k in ("company", "title", "location", "url", "posted")})
            new.append(ledger[key] | {"note": j["note"]})
        else:
            e = ledger[key]
            e.update(last_seen=TODAY, closed=None, title=j["title"], location=j["location"], url=j["url"])
            still.append(e)
    for key, e in ledger.items():
        board = ":".join(key.split(":")[:2])
        if key not in matched and not e.get("closed") and board in ok_boards:
            e["closed"] = TODAY
            closed.append(e)

    def line(e):
        age = f", posted {e['posted']}" if e.get("posted") else ""
        n = f" ({e['note']})" if e.get("note") else ""
        return f"- **{e['company']}**: {e['title']} | {e['location'] or 'n/a'}{age}{n}\n  {e['url']}"

    def block(title, items):
        body = "\n".join(line(e) for e in sorted(items, key=lambda x: (x["company"], x["title"]))) or "- none"
        return f"## {title} ({len(items)})\n{body}\n"

    head = (f"# Board scan {TODAY}\n\nBoards checked: {len(ok_boards)} of {len(tasks)}. "
            f"Postings read: {total}. Title matches kept: {len(matched)}. New: {len(new)}. Closed: {len(closed)}.\n")
    if baseline:
        head += "\nBaseline run: every open match is listed as new. Score only roles posted in the last 30 days.\n"
    err_txt = "\n".join(f"- {e}" for e in errors) or "- none"
    alerts = "\n".join([head, block("New since last run", new), block("Still open", still),
                        block("Closed since last run", closed), f"## Boards that failed ({len(errors)})\n{err_txt}\n"])

    drop_txt = "\n".join(f"- **{j['company']}**: {j['title']} | {j['location'] or 'n/a'} | {j['note']}\n  {j['url']}"
                         for j in sorted(dropped, key=lambda x: (x["company"], x["title"]))) or "- none"
    dropped_md = (f"# Dropped on location {TODAY}\n\nTitle matched a sales style role but the location rule rejected it. "
                  f"Read these for near misses (for example a role that allows remote Spain).\n\n{drop_txt}\n")

    for d in ("alerts", "dropped"):
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    open(os.path.join(ROOT, "alerts", f"{TODAY}.md"), "w").write(alerts)
    open(os.path.join(ROOT, "alerts", "latest.md"), "w").write(alerts)
    open(os.path.join(ROOT, "dropped", "latest.md"), "w").write(dropped_md)
    json.dump(ledger, open(ledger_path, "w"), indent=1, sort_keys=True, ensure_ascii=False)
    print(head)
    print(f"Dropped on location: {len(dropped)}. Failed boards: {len(errors)}")


if __name__ == "__main__":
    sys.exit(main())
