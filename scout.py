#!/usr/bin/env python3
"""
Job Scout scanner.

Reads companies.json, pulls each company's public job feed straight from its
hiring system (Greenhouse, Lever, Ashby, Workday, SmartRecruiters), keeps the
roles that fit Eddie's search, and writes:

  docs/jobs.json   everything the website shows (matches, skipped, problems)
  docs/feed.txt    plain-text list of fresh matches (read by the Claude task)
  data/state.json  memory of what was seen before (so "new" means new)

No third-party packages. Run:  python scout.py
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPANIES_FILE = ROOT / "companies.json"
STATE_FILE = ROOT / "data" / "state.json"
DOCS = ROOT / "docs"

# ---------------------------------------------------------------- settings
MIN_TOP_SALARY = 120_000   # keep a job when the TOP of its range reaches this
TOO_SENIOR_YEARS = 8       # skip when the posting asks for this many years or more
FRESH_DAYS = 7             # "new" window for feed.txt
WORKERS = 8
TIMEOUT = 25

SEARCH_KEYWORDS = [        # used for systems that only offer search (Workday, SmartRecruiters)
    "learning", "instructional", "enablement", "training",
    "talent development", "onboarding", "AI adoption",
]

TITLE_INCLUDE = re.compile(r"""(?ix)
    \blearning\b | \bl\s?&\s?d\b | \binstructional\b | \benablement\b
  | talent\s+development | \btraining\b | \btrainer\b | \bcurriculum\b
  | \bonboarding\b | customer\s+education | \beducation\b | \blms\b
  | organi[sz]ation(al)?\s+development | people\s+development
  | leadership\s+development | \bai\s+(adoption|fluency|literacy|transformation)\b
  | \badoption\b | change\s+management
""")

# Dropped quietly: not the kind of job this search is about.
TITLE_NOISE = re.compile(r"""(?ix)
    machine\s+learning | deep\s+learning | reinforcement\s+learning | \bml\b
  | engineer | scientist | research | \bintern\b | internship | \bco-?op\b
  | account\s+executive | account\s+manager | \bsdr\b | \bbdr\b | sales\s+development
  | recruit | counsel | attorney | paralegal | accountant | \btax\b | auditor
  | nurse | physician | clinical | medical\s+science | pharmac
  | teacher | tutor | professor | faculty | lecturer | \bdean\b
  | personal\s+trainer | fitness | athletic | driver | technician | mechanic
  | \barchitect\b | developer | software | security | devops | \bdata\s+enablement
  | product\s+manager | product\s+marketing | \bdesigner,?\s+product\b
  | customer\s+success | solutions? | payroll | benefits | compensation
  | contract(or)?\b | part[\s-]?time | temporary | freelance | apprentice
""")

# Recorded on the Skipped tab, so Eddie can overrule.
TITLE_SENIOR = re.compile(r"""(?ix)
    \bvp\b | \bsvp\b | \bevp\b | vice\s+president | head\s+of | \bchief\b
  | (?<!associate\s)\bdirector\b | \bprincipal\b | managing\s+director
""")

NY = re.compile(r"new\s+york|\bnyc\b|brooklyn|manhattan|\bqueens\b|jersey\s+city|hoboken|,\s*ny\b|\bny\b", re.I)
REMOTE = re.compile(r"remote|anywhere|distributed|work\s+from\s+home|virtual", re.I)
US_WORD = re.compile(r"united\s+states|north\s+america|\bamericas\b|nationwide", re.I)
US_ABBR = re.compile(r"\b(US|USA|U\.S\.A?\.?)\b")  # case-sensitive on purpose
NON_US = re.compile(r"""(?ix)
    canada|toronto|vancouver|montreal|united\s+kingdom|\buk\b|london|ireland|dublin
  | emea|apac|latam|europe|germany|berlin|munich|france|paris|spain|madrid|barcelona
  | netherlands|amsterdam|poland|warsaw|portugal|lisbon|italy|sweden|stockholm
  | india|bangalore|bengaluru|hyderabad|mumbai|pune|singapore|japan|tokyo|korea|seoul
  | australia|sydney|melbourne|new\s+zealand|brazil|mexico|argentina|colombia
  | israel|tel\s+aviv|philippines|manila|china|hong\s+kong|uae|dubai|south\s+africa
""")

_AMT = r"(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{2,3}(?:\.\d+)?\s?[kK]|\d{5,6})"
_SEP = r"\s*(?:USD|usd)?\s*(?:-|–|—|to|and)\s*(?:USD|usd)?\s*"
SALARY_RANGE = re.compile(r"\$\s?" + _AMT + _SEP + r"\$?\s?" + _AMT)
SALARY_RANGE_USD = re.compile(_AMT + _SEP + _AMT + r"\s*(?:USD|usd)")
YEARS = re.compile(
    r"(\d{1,2})\s*\+?(?:\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*years?[’']?\s+(?:of\s+)?(?:[\w&/,-]+\s+){0,5}?experience",
    re.I,
)
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


# ---------------------------------------------------------------- helpers
def today():
    return dt.datetime.now(dt.timezone.utc).date()


def http_json(url, body=None, timeout=TIMEOUT):
    """GET (or POST when body is given) and parse JSON. Raises on failure."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; personal-job-scout/1.0)",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 410, 422):
                raise RuntimeError(f"HTTP {e.code}")
            last = e
        except Exception as e:  # timeouts, resets, bad JSON
            last = e
    raise RuntimeError(str(last)[:120])


def clean(text):
    """HTML (possibly escaped twice) to plain text."""
    if not text:
        return ""
    t = html.unescape(html.unescape(str(text)))
    t = TAG.sub(" ", t)
    return WS.sub(" ", t).strip()


def _num(s):
    s = s.replace(",", "").strip()
    if s[-1] in "kK":
        return int(float(s[:-1]) * 1000)
    return int(float(s))


def parse_salary(text):
    """Find yearly pay ranges in text. Returns (low, high) or None."""
    lows, highs = [], []
    for rx in (SALARY_RANGE, SALARY_RANGE_USD):
        for m in rx.finditer(text or ""):
            try:
                a, b = _num(m.group(1)), _num(m.group(2))
            except ValueError:
                continue
            if b < 1000 and a >= 1000:      # "$120,000 - 150" style typo, ignore
                continue
            if a < 1000 and b < 1000 and not re.search(r"[kK]", m.group(0)):
                continue                    # hourly rates
            lo, hi = min(a, b), max(a, b)
            if 30_000 <= lo <= 700_000 and 30_000 <= hi <= 700_000:
                lows.append(lo)
                highs.append(hi)
    if not highs:
        return None
    return min(lows), max(highs)


def parse_years(text):
    """Largest 'N+ years ... experience' the posting asks for, or None."""
    vals = [int(m.group(1)) for m in YEARS.finditer(text or "")]
    vals = [v for v in vals if 1 <= v <= 20]
    return max(vals) if vals else None


def location_kind(loc):
    """'nyc', 'remote-us', 'remote?' (remote, country unclear) or None (not a fit)."""
    loc = loc or ""
    if NY.search(loc):
        return "nyc"
    if REMOTE.search(loc):
        if US_WORD.search(loc) or US_ABBR.search(loc):
            return "remote-us"
        if NON_US.search(loc):
            return None
        return "remote?"
    return None


def title_wanted(title):
    return bool(TITLE_INCLUDE.search(title or "")) and not TITLE_NOISE.search(title or "")


def fmt_salary(lo, hi):
    if not hi:
        return "Not listed"
    k = lambda v: f"${v/1000:.0f}k"
    return k(hi) if lo == hi else f"{k(lo)}–{k(hi).lstrip('$')}"


def iso_date(value):
    """Best-effort date string (YYYY-MM-DD) from the many formats feeds use."""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
    s = str(value)
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    m = re.search(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", s, re.I)
    if m:
        if m.group(1).lower() == "today":
            n = 0
        elif m.group(1).lower() == "yesterday":
            n = 1
        else:
            n = int(m.group(2))
        return (today() - dt.timedelta(days=n)).isoformat()
    return ""


def make_job(company, ats, jid, title, url, location, posted, text, sal=None):
    return {
        "key": f"{ats}:{company['name']}:{jid}",
        "company": company["name"],
        "ats": ats,
        "title": (title or "").strip(),
        "url": url,
        "location": WS.sub(" ", location or "").strip(" ;,|"),
        "posted": iso_date(posted),
        "_text": text or "",
        "_sal": sal,
    }


# ---------------------------------------------------------------- feeds
def fetch_greenhouse(c):
    slug = c["slug"]
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    listing = http_json(base).get("jobs", [])
    out = []
    for j in listing:
        if not title_wanted(j.get("title")):
            continue
        try:
            d = http_json(f"{base}/{j['id']}?pay_transparency=true")
        except RuntimeError:
            d = j
        locs = [(d.get("location") or {}).get("name", "")]
        locs += [o.get("name", "") for o in d.get("offices") or []]
        sal = None
        ranges = [r for r in d.get("pay_input_ranges") or [] if r.get("max_cents")]
        if ranges:
            sal = (min(r.get("min_cents", r["max_cents"]) for r in ranges) // 100,
                   max(r["max_cents"] for r in ranges) // 100)
        out.append(make_job(c, "greenhouse", j["id"], j.get("title"), j.get("absolute_url"),
                            "; ".join(dict.fromkeys(x for x in locs if x)),
                            d.get("first_published") or d.get("updated_at"),
                            clean(d.get("content")), sal))
    return out, len(listing)


def fetch_lever(c):
    listing = http_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in listing:
        if not title_wanted(j.get("text")):
            continue
        cat = j.get("categories") or {}
        locs = [cat.get("location", "")] + list(cat.get("allLocations") or [])
        if j.get("workplaceType") == "remote":
            locs.append("Remote")
        if j.get("country"):
            locs.append(j["country"])
        text = " ".join([j.get("descriptionPlain") or clean(j.get("description")),
                         " ".join(clean(l.get("text", "") + " " + l.get("content", "")) for l in j.get("lists") or []),
                         j.get("additionalPlain") or clean(j.get("additional")),
                         clean(j.get("salaryDescription"))])
        sal = None
        sr = j.get("salaryRange") or {}
        if sr.get("max") and "year" in str(sr.get("interval", "year")).lower():
            sal = (int(sr.get("min") or sr["max"]), int(sr["max"]))
        out.append(make_job(c, "lever", j.get("id"), j.get("text"), j.get("hostedUrl"),
                            "; ".join(dict.fromkeys(x for x in locs if x)), j.get("createdAt"), text, sal))
    return out, len(listing)


def fetch_ashby(c):
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=true")
    listing = data.get("jobs", [])
    out = []
    for j in listing:
        if j.get("isListed") is False or not title_wanted(j.get("title")):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        country = (((j.get("address") or {}).get("postalAddress")) or {}).get("addressCountry")
        if country:
            locs.append(country)
        comp = j.get("compensation") or {}
        sal = None
        vals = [(s.get("minValue"), s.get("maxValue")) for s in comp.get("summaryComponents") or []
                if s.get("compensationType") == "Salary" and s.get("maxValue")
                and "YEAR" in str(s.get("interval", "YEAR")).upper()]
        if vals:
            sal = (int(min(v[0] or v[1] for v in vals)), int(max(v[1] for v in vals)))
        text = (j.get("descriptionPlain") or clean(j.get("descriptionHtml"))) + " " + str(
            comp.get("compensationTierSummary") or "")
        out.append(make_job(c, "ashby", j.get("id"), j.get("title"), j.get("jobUrl"),
                            "; ".join(dict.fromkeys(x for x in locs if x)), j.get("publishedAt"), text, sal))
    return out, len(listing)


def fetch_workday(c):
    host = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{c['tenant']}/{c['site']}"
    found = {}
    for kw in SEARCH_KEYWORDS:
        for offset in (0, 20, 40):
            page = http_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": kw})
            posts = page.get("jobPostings") or []
            for p in posts:
                if p.get("externalPath"):
                    found.setdefault(p["externalPath"], p)
            if len(posts) < 20:
                break
    out = []
    for path, p in found.items():
        if not title_wanted(p.get("title")):
            continue
        loc, posted, text, url = p.get("locationsText", ""), p.get("postedOn", ""), "", f"{host}/en-US/{c['site']}{path}"
        try:
            info = http_json(f"{api}{path}").get("jobPostingInfo") or {}
            locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
            if info.get("remoteType"):
                locs.append(info["remoteType"])
            if (info.get("country") or {}).get("descriptor"):
                locs.append(info["country"]["descriptor"])
            loc = "; ".join(dict.fromkeys(x for x in locs if x)) or loc
            posted = info.get("startDate") or posted
            text = clean(info.get("jobDescription"))
            url = info.get("externalUrl") or url
        except RuntimeError:
            pass
        out.append(make_job(c, "workday", path.rsplit("/", 1)[-1], p.get("title"), url, loc, posted, text))
    return out, len(found)


def fetch_smartrecruiters(c):
    cid = c["slug"]
    api = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
    found = {}
    for kw in SEARCH_KEYWORDS:
        page = http_json(f"{api}?limit=100&q={urllib.parse.quote(kw)}")
        for p in page.get("content") or []:
            found.setdefault(p["id"], p)
    out = []
    for pid, p in found.items():
        if not title_wanted(p.get("name")):
            continue
        l = p.get("location") or {}
        loc = l.get("fullLocation") or ", ".join(x for x in [l.get("city"), l.get("region"), l.get("country")] if x)
        if l.get("remote"):
            loc += "; Remote"
        text, url = "", f"https://jobs.smartrecruiters.com/{cid}/{pid}"
        try:
            d = http_json(f"{api}/{pid}")
            secs = ((d.get("jobAd") or {}).get("sections")) or {}
            text = clean(" ".join((s or {}).get("text", "") for s in secs.values()))
            url = d.get("postingUrl") or url
        except RuntimeError:
            pass
        out.append(make_job(c, "smartrecruiters", pid, p.get("name"), url, loc, p.get("releasedDate"), text))
    return out, len(found)


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
}


# ---------------------------------------------------------------- judging
def judge(job):
    """Fill in salary/years/location fields. Returns a skip reason, or '' to keep."""
    text = job.pop("_text", "")
    sal = job.pop("_sal", None) or parse_salary(text)
    job["salary_min"], job["salary_max"] = (sal or (None, None))
    job["salary"] = fmt_salary(*(sal or (None, None)))
    job["years"] = parse_years(text)
    kind = location_kind(job["location"])
    if kind is None and re.search(r"remote", job["title"], re.I):
        kind = "remote?"
    job["where"] = kind or ""
    if kind is None:
        return "location"          # dropped quietly (most jobs are simply elsewhere)
    m = TITLE_SENIOR.search(job["title"])
    if m:
        return f"Too senior (title says {m.group(0).strip()})"
    if job["years"] and job["years"] >= TOO_SENIOR_YEARS:
        return f"Too senior (asks for {job['years']}+ years)"
    if sal and sal[1] < MIN_TOP_SALARY:
        return f"Pay too low (tops out at ${sal[1]/1000:.0f}k)"
    return ""


def scan_company(c):
    fetcher = FETCHERS.get(c.get("ats"))
    if not fetcher:
        return c, [], 0, f"unknown system '{c.get('ats')}'"
    try:
        jobs, total = fetcher(c)
        return c, jobs, total, ""
    except Exception as e:  # one bad company must never stop the run
        return c, [], 0, str(e)[:160]


# ---------------------------------------------------------------- main
def run(companies=None, state=None, write=True):
    if companies is None:
        companies = json.loads(COMPANIES_FILE.read_text())
    first_run = state is None and not STATE_FILE.exists()
    if state is None:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"jobs": {}}
    seen = state.setdefault("jobs", {})
    now = today().isoformat()

    matches, skipped, problems, scanned = [], [], [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(scan_company, c) for c in companies]
        for f in as_completed(futures):
            c, jobs, total, err = f.result()
            if err:
                problems.append({"company": c["name"], "ats": c.get("ats", ""), "error": err})
                continue
            scanned += total
            for job in jobs:
                reason = judge(job)
                if reason == "location":
                    continue
                if reason:
                    job["reason"] = reason
                    skipped.append(job)
                else:
                    matches.append(job)

    failed = {p["company"] for p in problems}
    live = set()
    new_keys = []
    for job in matches:
        key = job["key"]
        live.add(key)
        prev = seen.get(key)
        if prev:
            job["first_seen"] = prev.get("first_seen", "")
        else:
            # On the very first run we can't tell new from old unless the feed gives a date.
            job["first_seen"] = "" if first_run else now
            if not first_run:
                new_keys.append(key)
        seen[key] = {"first_seen": job["first_seen"], "last_seen": now,
                     "title": job["title"], "company": job["company"], "url": job["url"]}
        ref = job["posted"] or job["first_seen"]
        job["fresh"] = bool(ref) and (today() - dt.date.fromisoformat(ref)).days <= FRESH_DAYS

    # Forget jobs that vanished (closed), unless their company failed to load this time.
    for key in list(seen):
        if key not in live and seen[key].get("company") not in failed:
            last = seen[key].get("last_seen", now)
            if (today() - dt.date.fromisoformat(last)).days > 30:
                del seen[key]

    order = lambda j: (j["posted"] or j.get("first_seen") or "0000", j["company"])
    matches.sort(key=order, reverse=True)
    skipped.sort(key=order, reverse=True)
    problems.sort(key=lambda p: p["company"])

    result = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "companies_checked": len(companies) - len(problems),
        "companies_total": len(companies),
        "postings_scanned": scanned,
        "matches": matches,
        "skipped": skipped,
        "problems": problems,
    }
    if write:
        DOCS.mkdir(exist_ok=True)
        STATE_FILE.parent.mkdir(exist_ok=True)
        (DOCS / "jobs.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        (DOCS / "feed.txt").write_text(feed_text(result))
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
        notify([j for j in matches if j["key"] in new_keys])
    return result


def feed_text(result):
    fresh = [j for j in result["matches"] if j["fresh"]]
    lines = [
        f"JOB SCOUT FEED generated {result['generated']}",
        f"Checked {result['companies_checked']} of {result['companies_total']} companies. "
        f"{len(result['matches'])} open matches, {len(fresh)} fresh (last {FRESH_DAYS} days).",
        "Each line: posted date | company | title | location | salary | years asked | url",
        "",
    ]
    for j in fresh:
        lines.append(" | ".join([j["posted"] or j["first_seen"] or "?", j["company"], j["title"],
                                 j["location"], j["salary"],
                                 f"{j['years']}+ yrs" if j["years"] else "yrs n/a", j["url"]]))
    if not fresh:
        lines.append("(no fresh matches)")
    return "\n".join(lines) + "\n"


def notify(new_jobs):
    """Optional phone push through ntfy.sh. Set NTFY_TOPIC to turn it on."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not new_jobs:
        return
    body = "\n".join(f"{j['company']}: {j['title']} ({j['salary']})" for j in new_jobs[:8])
    if len(new_jobs) > 8:
        body += f"\n…and {len(new_jobs) - 8} more"
    headers = {"Title": f"Job Scout: {len(new_jobs)} new", "Tags": "briefcase"}
    site = os.environ.get("SITE_URL", "").strip()
    if site:
        headers["Click"] = site
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers)
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    r = run()
    print(f"Checked {r['companies_checked']}/{r['companies_total']} companies, "
          f"{r['postings_scanned']} postings scanned, {len(r['matches'])} matches, "
          f"{len(r['skipped'])} skipped, {len(r['problems'])} problems.")
    for p in r["problems"]:
        print(f"  problem: {p['company']} ({p['ats']}): {p['error']}")
