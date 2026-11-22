"""
California election results  ->  Google Sheets            (written for the Nov. 3, 2026 general election)
=====================================================================================================

HOW THE PIECES FIT TOGETHER
---------------------------
    GitHub Action (.github/workflows/update_elections.yml, every 30 minutes, or "Run workflow" by hand)
        |
        v
    main.py  (this file)
        1. downloads results from each county / the Secretary of State  -> one list of rows per tab
        2. throws away any source that failed or came back empty (that tab keeps its LAST GOOD data)
        3. POSTs every good tab, plus a STATUS tab, to the Google Apps Script webhook
        |
        v
    Apps Script "Election Results Webhook" (Code.gs)
        4. writes each tab into the spreadsheet (creating it if it is new)
        5. rebuilds the RESULTS tab: one tidy table of every RAW_* / RCV_* tab
        |
        v
    The ALL tab looks candidates up in RESULTS (by contest key + candidate name), never by cell address.

TAB NAMING:  RAW_<place>      = what the county published (one row per candidate/choice)
             RCV_<race>       = round-by-round ranked-choice results for one race
             RAW_Unprocessed  = the counties' own estimates of ballots left to count (from the Secretary of State)
             RESULTS          = built by Apps Script from all of the above (do not edit by hand)
             COMBINED         = a small tab YOU edit: measures that span several counties, summed into RESULTS
             STATUS           = written by this file: OK / EMPTY / ERROR for every source, every run

HOW OFTEN IT RUNS:  only when you ask. In the spreadsheet: Elections > Update results now (that tells GitHub to run this
                    file). Or in GitHub: Actions > Update California Election Results > Run workflow.

WHERE TO ADD THINGS  (each is a one-line edit in the CONFIG block below)
    another ranked-choice (RCV) tab ............. RCV_ALAMEDA
    another Clarity county (like Butte) ......... CLARITY_SOURCES
    another livevoterturnout.com county ......... LVT_CLEAN_SOURCES  (HTML pages)  or  LVT_XML_SOURCES (XML feed)

NEXT-ELECTION CHECKLIST  (e.g. the 2028 primary) - what is tied to the 2026 general election
    1. CONFIG block: ALAMEDA_ELECTION, RCV_ALAMEDA, CLARITY_SOURCES ids, LVT_* urls, SF_RESULTS_URL.
    2. A few older fetchers still have the election hard-coded INSIDE them. Search this file for:
           "2026nov03"        fetch_santa_barbara   (Santa Barbara results page)
           "electionID=4348"  fetch_la              (LA County election id - appears twice)
           "/rovresults/260/" fetch_alameda_main    (Alameda election number)
           "sandiegocaenr/25" LVT_CLEAN_SOURCES     (San Diego livevoterturnout id - now in the CONFIG block)
           "eresults.saccounty.net"  fetch_sacramento_web (same address every election; confirm it shows the new one)
    3. UBS_URL / SOURCE_COUNTIES (the "ballots left" report): the page address is normally stable, but confirm the
       table still has the columns "Estimated Total Ballots Remaining" and "Last Report". Add every new RAW_ tab to
       SOURCE_COUNTIES so its county's figures get attached.
    3b. SOS_FILES: the offices on the ballot change by year. Compare with the list at
       https://media.sos.ca.gov/media/json-endpoints.csv (it names every statewide feed for the current election).
    4. Look at the STATUS tab after the first run - any ERROR / EMPTY row tells you which source to fix.

WHY THE "previous data kept" BEHAVIOR MATTERS
    Counties go down, change layouts, or block bots, usually on election night. A failed source must never blank
    out good data, so failures are skipped (and reported in STATUS) instead of being written as empty tabs.
"""
import base64
import gzip
import io
import json
import os
import re
import sys
import time
import zipfile
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, unquote
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# The Apps Script web-app URL and the shared secret come from GitHub "Actions secrets" (see the workflow file).
WEBHOOK_URL = os.environ["APPS_SCRIPT_URL"]
SECRET_TOKEN = os.environ["WEBHOOK_SECRET"]

# Every "Updated" stamp is Pacific time. (Until Nov. 19 it was the GitHub runner's clock, which is UTC and so looked
# 8 hours ahead. Google Sheets reads these strings in the spreadsheet's own time zone, so keep that set to Pacific.)
NOW = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")
NOW_PT = NOW  # same thing; kept as a separate name because the STATUS tab uses it

# A normal-looking browser identity. Some county sites refuse the default python-requests identity.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xml, text/plain, */*",
}

# =====================================================================================
# CONFIG - the parts you will edit during an election
# =====================================================================================

# --- Ranked-choice races in Alameda County (Oakland / Berkeley / Albany) ---------------------------------
#   key   = the Google Sheet tab name
#   value = the race code: the text after "race=" in the county's "RCV tabulations" link on
#           https://www.alamedacountyca.gov/rovresults/260/   (with %2F turned back into "/")
# Every run also logs the races Alameda lists that are NOT in this dict yet, so you can see what else exists.
ALAMEDA_ELECTION = 260  # the "260" in .../rovresults/260/  (changes every election)
RCV_ALAMEDA = {
    "RCV_Alameda":          "Oakland/008-SchoolDirector_District_6",   # the original tab (Oakland School Board D6)
    "RCV_Oakland_D2":       "Oakland/002-CityCouncil_District_2",
    "RCV_Berkeley_D7":      "Berkeley/003-CityCouncil_District_7",
    "RCV_Albany_Council":   "Albany/001-CityCouncil",
    "RCV_Oakland_SchoolD2": "Oakland/006-SchoolDirector_District_2",
    # Other RCV races Alameda lists - remove the "#" to add one:
    # "RCV_Oakland_Mayor":      "Oakland/001-Mayor",
    # "RCV_Oakland_D4":         "Oakland/003-CityCouncil_District_4",
    # "RCV_Oakland_D6":         "Oakland/004-CityCouncil_District_6",
    # "RCV_Oakland_Auditor":    "Oakland/005-CityAuditor",
    # "RCV_Oakland_SchoolD4":   "Oakland/007-SchoolDirector_District_4",
    # "RCV_Berkeley_D1":        "Berkeley/001-CityCouncil_District_1",
    # "RCV_Berkeley_D4":        "Berkeley/002-CityCouncil_District_4",
    # "RCV_Berkeley_D8":        "Berkeley/004-CityCouncil_District_8",
    # "RCV_Berkeley_Auditor":   "Berkeley/005-CityAuditor",
    # "RCV_Albany_Schools":     "Albany/002-BoardOfEducation",
}

# --- Counties on Clarity Elections:  https://results.enr.clarityelections.com/CA/<County>/<election id>/ ----
#   tab name -> (County exactly as written in that URL, Clarity election id)
# Clarity sits behind Amazon's bot protection, which sometimes answers scripts with "HTTP 202" instead of the data.
# fetch_clarity() therefore tries an ordinary request first and, if that is refused, a real headless browser.
CLARITY_SOURCES = {
    "RAW_Butte":       ("Butte", "126778"),         # id confirmed on buttevotes.ca.gov's Nov 3, 2026 page
    "RAW_ContraCosta": ("Contra_Costa", "127405"),  # note the underscore in the URL
    "RAW_SantaClara":  ("Santa_Clara", "127570"),   # needed for the five-county regional measure (RTM)
}

# --- Counties on livevoterturnout.com whose HTML pages we read (same vendor as San Diego) ----------------
#   tab name -> Index page URL.   (San Diego is here too, so it also gets the XML top-up.)  Names are tidied ("X (DEM) X, DEM" -> "X") and a Party column is added.
LVT_CLEAN_SOURCES = {
    "RAW_Riverside":     "https://www.livevoterturnout.com/ENR/riversidecaenr/12/en/Index_12.html",
    "RAW_SanBernardino": "https://www.livevoterturnout.com/ENR/sanbernardinocaenr/3/en/Index_3.html",
    "RAW_Imperial":      "https://www.livevoterturnout.com/ENR/imperialcaenr/6/en/Index_6.html",
    "RAW_SanDiego":      "https://www.livevoterturnout.com/ENR/sandiegocaenr/25/en/Index_25.html",
}

# --- Counties on livevoterturnout.com that also publish a clean XML summary feed (Orange County) ----------
#   tab name -> URL of summary_<id>.xml  (the same file name pattern probably exists for the other
#   livevoterturnout.com counties above - a sturdier source than their HTML if those pages ever change).
LVT_XML_SOURCES = {
    "RAW_OrangeCounty": "https://www.livevoterturnout.com/ENR/orangecaenr/24/summary_24.xml",
}

# --- "How many ballots are left?" -------------------------------------------------------------------------------
# The Secretary of State publishes the counties' own estimates (ballots processed, ballots remaining, when each county
# last reported and when it expects to report next) on a web page - there is no data feed for it, so a browser reads it.
UBS_URL = "https://electionresults.sos.ca.gov/unprocessed-ballots-status"
INCLUDE_CURE_IN_LEFT = False   # True = also count "ballots left to cure" (signature problems) as still-to-come

# Which county (or counties) each RAW_ tab covers, so the right "ballots left" figure and "last reported" time can be
# attached to it. County names are spelled as the SOS page spells them. RCV_ tabs are Alameda County automatically.
SOURCE_COUNTIES = {
    "RAW_SantaBarbara": ["Santa Barbara"], "RAW_Alameda": ["Alameda"], "RAW_LA": ["Los Angeles"], "RAW_SF": ["San Francisco"],
    "RAW_SanDiego": ["San Diego"], "RAW_Butte": ["Butte"], "RAW_ContraCosta": ["Contra Costa"], "RAW_Riverside": ["Riverside"],
    "RAW_SanBernardino": ["San Bernardino"], "RAW_Imperial": ["Imperial"], "RAW_OrangeCounty": ["Orange"],
    "RAW_Sacramento": ["Sacramento"], "RAW_SantaClara": ["Santa Clara"],
}

# San Francisco: the numbers are on index.html (detail.html is only a menu of PDF/Excel links).
SF_RESULTS_URL = "https://www.sfelections.org/results/20261103/index.html"

# --- California Secretary of State statewide results -------------------------------------------------------
# (file name on media.sos.ca.gov, matching api.sos.ca.gov path, required?)
# "required" files must ALL load or the RAW_Statewide tab is left untouched; optional files only log a warning.
# The media.sos.ca.gov copy is meant for scripts; api.sos.ca.gov (same data) is the fallback.
SOS_MEDIA = "https://media.sos.ca.gov/media"
SOS_API = "https://api.sos.ca.gov/returns"
SOS_FILES = [
    ("governor",                             "governor",                            True),
    ("lieutenant-governor",                  "lieutenant-governor",                 True),
    ("secretary-of-state",                   "secretary-of-state",                  True),
    ("controller",                           "controller",                          True),
    ("treasurer",                            "treasurer",                           True),
    ("attorney-general",                     "attorney-general",                    True),
    ("insurance-commissioner",               "insurance-commissioner",              True),
    ("superintendent-of-public-instruction", "superintendent-of-public-instruction", True),
    ("ballot-measures",                      "ballot-measures",                     True),
    ("us-rep",                               "us-rep/district/all",                 True),
    ("state-senate",                         "state-senate/district/all",           True),
    ("state-assembly",                       "state-assembly/district/all",         True),
    ("board-of-equalization",                "board-of-equalization/district/all",  False),
    ("supreme-court",                        "supreme-court",                       False),   # judges: log says "no whole-race results" - harmless
    ("court-of-appeal",                      "courts-of-appeal/district/all",       False),   # judges: same
]

# =====================================================================================
# SMALL HELPERS (used by the newer fetchers further down)
# =====================================================================================

# Fetchers can leave a short remark for the STATUS tab here (key = tab name); run_source() picks it up.
SOURCE_NOTES = {}


def strip_ns(tag: str) -> str:
    """'{http://x}Foo' -> 'Foo'  (XML tags sometimes carry a namespace we do not care about)."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def clean(s) -> str:
    """Collapse all whitespace runs to single spaces and trim."""
    return re.sub(r"\s+", " ", s or "").strip()


def to_int(x):
    """'1,234' / '1234' / 1234.0 -> 1234.  Anything non-numeric -> None."""
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).replace(",", "").strip()
    return int(float(s)) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def pct_text(x) -> str:
    """'65.6' -> '65.6%'  (Google Sheets then stores a real percentage, like your other tabs)."""
    s = clean(str(x)) if x is not None else ""
    if not s:
        return ""
    return s if s.endswith("%") else f"{s}%"


def http_get(url, *, retries=3, timeout=30, headers=None):
    """GET with retries. Raises RuntimeError with a message that says exactly what the server did.

    Retried:      network errors, HTTP 5xx, 429 (rate limit) and 202.
    Not retried:  other 4xx (a 404 will not fix itself).
    HTTP 202 deserves a note: Amazon's bot protection (Clarity sits behind it) answers "prove you are a browser"
    with 202 + a tiny challenge page instead of the file.  Only a real browser can answer that - see BrowserGetter.
    """
    last = "no attempt made"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            waf = r.headers.get("x-amzn-waf-action")
            last = f"HTTP {r.status_code} from {url} (server={r.headers.get('server')}" + (f", waf-action={waf}" if waf else "") + ")"
            if r.status_code < 500 and r.status_code not in (202, 429):
                break  # 4xx: retrying will not help
        except requests.RequestException as e:
            last = f"{type(e).__name__} on {url}: {e}"
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError(last)


def maybe_gunzip(b: bytes) -> bytes:
    """Un-gzip bytes if (and only if) they start with the gzip magic number.
    Some servers send gzip WITHOUT the Content-Encoding header, so requests does not un-gzip it for us."""
    return gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b


def body_bytes(resp) -> bytes:
    return maybe_gunzip(resp.content)


def json_from(resp):
    return json.loads(body_bytes(resp).decode("utf-8-sig"))


def normalize(rows):
    """Apps Script's setValues() needs every row to be the same width, and no None values."""
    width = max(len(r) for r in rows)
    return [[("" if v is None else v) for v in r] + [""] * (width - len(r)) for r in rows]


# =====================================================================================
# 1. EXISTING FETCHERS - unchanged logic (only comments were added)
#    These write the tabs your ALL formulas used to point at by cell address (RAW_Alameda!D234 etc.),
#    so their row layout is deliberately NOT touched. Once ALL uses RESULTS lookups it no longer matters.
#    Each returns:  [header row, then one row per candidate/choice]
# =====================================================================================

# WORKING: Santa Barbara - one static HTML page; contests are single-cell "(Vote for N)" rows, then candidate rows.
def fetch_santa_barbara():
    url = "https://www.sbcvote.com/Elections/results/2026nov03/results-1.htm"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    current_contest = ""

    for tr in soup.find_all("tr"):
        if tr.find("table"):
            continue
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if not cells:
            continue

        if len(cells) == 1 and "(Vote for" in cells[0]:
            current_contest = cells[0]
            continue

        if current_contest and len(cells) >= 5 and cells[0] not in ("Candidate", "Times Cast"):
            cand = cells[0]
            total_votes = cells[3].replace(",", "")
            pct = cells[4]
            if total_votes.isdigit():
                rows.append([current_contest, cand, int(total_votes), pct, NOW])

    return rows


# WORKING: Alameda Main - the county results page; each contest is a "panelDtl" block under a heading.
def fetch_alameda_main():
    url = "https://www.alamedacountyca.gov/rovresults/260/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for panel in soup.select("div.panelDtl"):
        trigger = panel.find_previous(["button", "h3", "h4"])
        contest = re.sub(r"\s+", " ", trigger.get_text(" ", strip=True)) if trigger else ""
        if not contest:
            continue

        for tr in panel.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                pcts = [c for c in cells[1:] if "%" in c]
                if nums and cells[0].lower() not in ("candidate", "total", "total votes", "registered voters", "ballots cast", "undervotes", "overvotes"):
                    rows.append([contest, cells[0], int(nums[-1]), pcts[-1] if pcts else "", NOW])
    return rows


# WORKING: Sacramento Web - JavaScript page, so it needs the Playwright browser (passed in as `page`).
# (This tab has some junk rows from nested HTML tables, but I left it alone on purpose:
#  cleaning it up would shift the row numbers your old ALL formulas point at. RESULTS filters the junk out.)
def fetch_sacramento_web(page):
    page.goto("https://eresults.saccounty.net/", wait_until="networkidle", timeout=30000)
    soup = BeautifulSoup(page.content(), "html.parser")

    rows = [["Contest", "Candidate", "Party", "Votes", "Pct", "Updated"]]
    for header in soup.find_all(["h2", "h3", "h4", "th", "td", "div", "span"]):
        txt = re.sub(r"\s+", " ", header.get_text(" ", strip=True))
        if "(Vote for" in txt and len(txt) < 150:
            tbl = header.find_next("table")
            while tbl and "Candidate" not in tbl.get_text():
                tbl = tbl.find_next("table")
            if not tbl:
                continue
            for tr in tbl.find_all("tr"):
                cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                cells = [c for c in cells if c]
                if len(cells) >= 3 and cells[0] not in ("Candidate", "Total Votes", "Times Cast", "Undervotes", "Overvotes"):
                    nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                    pcts = [c for c in cells[1:] if "%" in c]
                    party = cells[1] if len(cells) >= 4 and not cells[1].replace(",", "").isdigit() else ""
                    if nums:
                        rows.append([txt, cells[0], party, int(nums[-1]), pcts[-1] if pcts else "", NOW])
    return rows


# WORKING: Los Angeles - the county's own JSON API: one call for the contest list, one for the live vote counters.
def fetch_la():
    base = "https://results.lavote.gov/ElectionResults"
    hdrs = {**HEADERS, "Referer": "https://results.lavote.gov/"}

    meta = requests.get(f"{base}/GetElectionData?electionID=4348", headers=hdrs, timeout=20).json().get("Data", {})
    counter_raw = requests.get(f"{base}/GetCounterData?electionID=4348", headers=hdrs, timeout=20).json()
    counters = counter_raw.get("Data", counter_raw)

    cand_votes = {}
    if isinstance(counters, list):
        for item in counters:
            if isinstance(item, dict) and item.get("ReferenceType") == "CAND":
                ref_id = item.get("ReferenceID")
                val = item.get("Value")
                if ref_id is not None and isinstance(val, (int, float)):
                    cand_votes[ref_id] = int(val)

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for grp in meta.get("ContestGroups") or []:
        if not isinstance(grp, dict):
            continue
        for contest in grp.get("Contests") or []:
            if not isinstance(contest, dict):
                continue
            c_title = contest.get("Title") or contest.get("Name") or ""
            cands = contest.get("Candidates") or []
            resolved = []
            for cand in cands:
                if not isinstance(cand, dict):
                    continue
                cid = cand.get("ID")
                votes = cand_votes.get(cid, int(cand.get("Votes") or 0))
                resolved.append((cand.get("Name") or "", votes))

            total_votes = sum(v for _, v in resolved)
            for name, votes in resolved:
                pct = f"{(votes / total_votes) * 100:.2f}%" if total_votes > 0 else ""
                rows.append([c_title, name, votes, pct, NOW])
    return rows


# WORKING: livevoterturnout.com multi-page static HTML crawler (San Diego).
# Starts at the Index page, follows links to category pages (if any), and reads every results table.
# A contest's name is taken from the heading of the panel/card that wraps its table.
def fetch_lvt_multipage(index_url: str):
    resp = requests.get(index_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Collect all category HTML pages linked on Index_XX.html
    page_urls = [index_url]
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.endswith(".html") and not href.startswith("http") and "Summary" not in href:
            full_url = urljoin(index_url, href)
            if full_url not in page_urls:
                page_urls.append(full_url)

    print(f"LVT crawling {len(page_urls)} sub-pages:", [u.split("/")[-1] for u in page_urls])

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    seen = set()

    for page_url in page_urls:
        p_resp = requests.get(page_url, headers=HEADERS, timeout=20)
        if p_resp.status_code != 200:
            continue
        p_soup = BeautifulSoup(p_resp.text, "html.parser")

        for tbl in p_soup.find_all("table"):
            wrapper = tbl.find_parent(class_=re.compile(r"panel|card|contest|box", re.I))
            heading = wrapper.find(class_=re.compile(r"heading|title|header", re.I)) if wrapper else None
            if not heading:
                heading = tbl.find_previous(["h2", "h3", "h4", "button", "a"])
            contest = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)) if heading else ""
            if not contest or len(contest) > 150:
                continue

            for tr in tbl.find_all("tr"):
                cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                cells = [c for c in cells if c]
                if len(cells) >= 3 and cells[0].lower() not in ("candidate", "choice", "total", "total votes", "times cast", "undervotes", "overvotes"):
                    nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                    pcts = [c for c in cells[1:] if "%" in c]
                    if nums:
                        key = (contest, cells[0])
                        if key not in seen:
                            seen.add(key)
                            rows.append([contest, cells[0], int(nums[-1]), pcts[-1] if pcts else "", NOW])

    return rows


# =====================================================================================
# 2. Riverside / San Bernardino / Imperial  (livevoterturnout.com HTML, same crawler as San Diego)
#    The raw crawl doubles every name ("X (DEM) X, DEM", "YES YES"); these tabs get tidy names instead.
#    Your San Diego tab is left exactly as it was (RESULTS tidies it up the same way).
# =====================================================================================

def clean_lvt_contest(raw: str) -> str:
    """'Contest: GOVERNOR, VOTE FOR 1 GOVERNOR (VOTE FOR 1)'  ->  'GOVERNOR (Vote for 1)'.
    Anything that does not match that doubled pattern is returned unchanged (only whitespace-cleaned)."""
    s = clean(raw)
    s = re.sub(r"^Contest:\s*", "", s, flags=re.I)
    m = re.match(r"^(?P<t>.+?),\s*VOTE FOR (?P<n>\d+)\s+(?P=t)\s*\(VOTE FOR (?P=n)\)$", s, flags=re.I)
    if m:
        return f"{m.group('t').strip()} (Vote for {m.group('n')})"
    return s


def clean_lvt_candidate(raw: str):
    """Returns (name, party).
         'COREY A JACKSON (DEM) COREY A JACKSON, DEM'  ->  ('COREY A JACKSON', 'DEM')
         'YES YES'                                     ->  ('YES', '')
    A leading '*' (counties use it to mark incumbents) is dropped: in a spreadsheet lookup '*' is a wildcard."""
    s = clean(raw)
    m = re.match(r"^(?P<n>.+?)\s*\((?P<p>[^()]*)\)\s*(?P=n),\s*[A-Za-z .\-]+$", s)
    if m:
        name, party = m.group("n").strip(), m.group("p").strip()
    else:
        m = re.match(r"^(?P<n>.+?)\s+(?P=n)$", s)
        name, party = (m.group("n").strip(), "") if m else (s, "")
    return name.lstrip("*").strip(), party


def norm_contest(s: str) -> str:
    """Contest name -> comparison key: lower case, no '(Vote for N)', no punctuation. Used only to decide whether two
    descriptions of the SAME contest (page vs XML feed) match."""
    s = re.sub(r"\(\s*vote for \d+\s*\)|,?\s*vote for \d+", " ", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def lvt_xml_url(index_url: str):
    """.../ENR/riversidecaenr/12/en/Index_12.html  ->  .../ENR/riversidecaenr/12/summary_12.xml  (None if not that shape)."""
    u = re.sub(r"/en/Index_(\d+)\.html$", r"/summary_\1.xml", index_url)
    return u if u != index_url else None


def fetch_lvt_clean(index_url: str, note_key=None):
    """Riverside / San Bernardino / Imperial.

    1. Runs the proven page crawler (the same one San Diego uses) and tidies the names. These rows come FIRST and keep
       their names, so keys you already built keep working.
    2. The crawler misses some contests on these big pages (county boards of education, some school districts, some
       city races). livevoterturnout.com also publishes the complete data as an XML feed, so any contest that is in the
       XML but NOT on the page is appended after the page rows.
    Safety rule: if fewer than half of the XML's contests can be matched by name to the page, the two sources name
    things differently, so nothing is merged (that would create duplicates). The STATUS tab says which case happened."""
    raw = fetch_lvt_multipage(index_url)  # the proven crawler above
    html_rows = []
    for contest, cand, votes, pct, updated in raw[1:]:
        name, party = clean_lvt_candidate(cand)
        html_rows.append([clean_lvt_contest(contest), name, votes, pct, updated, party])

    extra, generated, note = [], "", ""
    xml_url = lvt_xml_url(index_url)
    if xml_url:
        try:
            xml_rows, generated = lvt_xml_fetch(xml_url)
            html_names = {norm_contest(r[0]) for r in html_rows}
            by_contest = {}
            for r in xml_rows:
                by_contest.setdefault(norm_contest(r[0]), []).append(r[:6])
            matched = sum(1 for k in by_contest if k in html_names)
            if by_contest and matched / len(by_contest) >= 0.5:
                new = [k for k in by_contest if k not in html_names]
                for k in new:
                    extra.extend(by_contest[k])
                note = f"page {len(html_rows)} rows + {len(extra)} rows from {len(new)} contests found only in the XML feed"
            else:
                note = f"XML feed found, but only {matched} of its {len(by_contest)} contests match the page by name - not merged"
        except Exception as e:
            note = f"XML feed not used ({clean(str(e))[:90]}); page rows only"
    if note_key:
        SOURCE_NOTES[note_key] = note
    print(f"[INFO] {note_key or index_url}: {note}")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party", "Source Updated"]]
    rows.extend(r + [generated] for r in html_rows + extra)
    return rows


# =====================================================================================
# 3. Orange County  (livevoterturnout.com's XML summary feed - structured, so no HTML guessing)
#    One <Table> element per candidate: RaceName, ContestantName, Party, TotalVotes, ContestantVotePercent ...
# =====================================================================================

def _child_text(el, name: str) -> str:
    for ch in el:
        if strip_ns(ch.tag) == name:
            return clean(ch.text or "")
    return ""


def lvt_xml_rows(root):
    """Parse a livevoterturnout.com summary XML into rows
    [contest, candidate, votes, pct, updated, party, reporting, source_updated] and the file's generated-at time."""
    source_updated = ""
    gen = _child_text(root, "GeneratedDate")  # e.g. 2026-11-13T16:54:35.314-08:00  (already Pacific time)
    if gen:
        source_updated = gen[:16].replace("T", " ")
    rows = []
    for el in root.iter():
        if strip_ns(el.tag) != "Table":
            continue
        contest = _child_text(el, "RaceName")
        name = _child_text(el, "ContestantName").lstrip("*").strip()   # '*' marks incumbents; also a sheet wildcard
        party = _child_text(el, "Party")
        if party:  # 'XAVIER BECERRA (DEM)' -> 'XAVIER BECERRA' (the party has its own column)
            name = re.sub(rf"\s*\({re.escape(party)}\)\s*$", "", name)
        votes = to_int(_child_text(el, "TotalVotes"))
        if not contest or not name or votes is None:
            continue
        reporting = f"{_child_text(el, 'PrecReported')} of {_child_text(el, 'NumberOfPrec')} precincts"
        rows.append([contest, name, votes, _child_text(el, "ContestantVotePercent"), NOW, party, reporting, source_updated])
    return rows, source_updated


def lvt_xml_fetch(summary_url: str):
    # The "v=" number is a cache-buster (the county's own page sends one too); a fresh value every run.
    resp = http_get(f"{summary_url}?v={int(time.time() * 1000)}", timeout=60)
    rows, generated = lvt_xml_rows(ET.fromstring(body_bytes(resp)))
    if not rows:
        raise RuntimeError("the XML feed loaded but contained no results")
    return rows, generated


def fetch_lvt_xml(summary_url: str):
    rows, _ = lvt_xml_fetch(summary_url)
    return [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party", "Reporting", "Source Updated"]] + rows


# =====================================================================================
# 4. San Francisco
#    Your old URL (detail.html) is only a menu of PDF/Excel links, which is why it parsed to 0 rows.
#    The numbers live on index.html: one <h4> per contest followed by one results table.
#    Ranked-choice contests show "Round 1" and the last round; the Round column says which one a row shows
#    (finalists show the LAST round; an eliminated candidate shows Round 1 plus "eliminated in Round N").
# =====================================================================================

def fetch_sf():
    resp = http_get(SF_RESULTS_URL, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    # "Last Update: November 10, 2026 4:00 PM" appears near the top of the page.
    source_updated = ""
    m = re.search(r"Last Update:\s*([A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)", soup.get_text(" ", strip=True))
    if m:
        try:
            source_updated = datetime.strptime(m.group(1), "%B %d, %Y %I:%M %p").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            source_updated = m.group(1)

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Round", "Source Updated"]]
    contest = None
    for el in soup.find_all(["h4", "table"]):          # walk headings and tables in page order
        if el.name == "h4":
            contest = clean(el.get_text(" ", strip=True))
            continue
        if not contest:
            continue  # tables before the first contest heading (the turnout summary)
        trs = el.find_all("tr")
        if not trs:
            continue
        first = [clean(c.get_text(" ", strip=True)) for c in trs[0].find_all(["th", "td"])]
        has_header = any(h.lower().startswith(("ballots counted", "round", "percentage")) for h in first)
        is_rcv = has_header and any(re.match(r"round\s*\d+", h, re.I) for h in first)
        body = trs[1:] if has_header else trs

        for tr in body:
            cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            name = cells[0]
            low = name.lower()
            if low == "total" or low.startswith(("under vote", "over vote", "undervote", "overvote")):
                continue

            if not is_rcv:  # ordinary contest: [name, votes, pct]
                votes = to_int(cells[1])
                if votes is None:
                    continue
                rows.append([contest, name, votes, cells[2] if len(cells) > 2 else "", NOW, "", source_updated])
            else:           # ranked-choice: one cell per round, like "23,059 (83.94%)" or "*eliminated in Round 2*"
                votes, pct, label, elim = None, "", "", ""
                for header, cell in zip(first[1:], cells[1:]):
                    mm = re.match(r"^([\d,]+)\s*\(([\d.]+%)\)", cell)
                    if mm:
                        votes, pct, label = to_int(mm.group(1)), mm.group(2), header
                    me = re.search(r"eliminated in Round (\d+)", cell, re.I)
                    if me:
                        elim = f"eliminated in Round {me.group(1)}"
                if votes is None:
                    continue
                rows.append([contest, name, votes, pct, NOW, f"{label}; {elim}" if elim else label, source_updated])
        contest = None  # a heading owns only the first table after it

    if len(rows) == 1:
        print("SF index.html snippet:", clean(soup.get_text())[:400])
    return rows


# =====================================================================================
# 5. Clarity Elections counties (Butte, Contra Costa)
#
#    What the data is:   <root>/current_ver.txt  -> a version number (changes on every upload)
#                        <root>/<ver>/json/en/summary.json -> every contest with candidates and vote counts
#                        <root>/<ver>/reports/detailxml.zip -> same data as XML (our fallback)
#
#    What went wrong on Nov 15: Clarity sits behind Amazon CloudFront + AWS WAF bot protection. When it does not
#    like the visitor it returns "HTTP 202" and a challenge page instead of the file. python-requests cannot answer
#    the challenge; a real browser can.  Whether a given run is challenged seems to depend on the runner's IP
#    address, which changes from run to run - that is why Butte worked once and then failed.
#
#    So every Clarity fetch goes through a "getter" (url -> bytes):
#        plain_getter     ordinary HTTP request. Fast. Tried first.
#        BrowserGetter    headless Chromium (already installed for Sacramento). Used only when plain was refused.
# =====================================================================================

CLARITY_ROOT = "https://results.enr.clarityelections.com/CA"


def clarity_rows_from_json(data):
    """summary.json -> rows.  Keys used: C = contest title, CH = candidate names, V = vote counts.
    Percentages are computed from the votes (so we never depend on what Clarity calls its percent field)."""
    contests = data
    if isinstance(data, dict):  # tolerate a {"Contests": [...]}-style wrapper
        contests = next((v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party"]]
    for c in contests or []:
        if not isinstance(c, dict):
            continue
        title = clean(str(c.get("C") or c.get("Name") or c.get("name") or c.get("title") or ""))
        names = c.get("CH") or c.get("Choices") or c.get("choices") or c.get("candidates") or []
        votes = c.get("V") or c.get("Votes") or c.get("votes") or []
        parties = c.get("PA") or c.get("Parties") or c.get("P") or []
        names = [n if isinstance(n, str) else (n.get("N") or n.get("Name") or n.get("name") or "") for n in names]
        vlist = [to_int(v) or 0 for v in votes]
        if not title or not names or len(names) != len(vlist):
            continue
        total = sum(vlist)
        for i, (n, v) in enumerate(zip(names, vlist)):
            party = parties[i] if i < len(parties) else ""
            party = party if isinstance(party, str) and not re.fullmatch(r"[\d.,%\s]*", party) else ""  # 'P' can hold percents
            rows.append([title, clean(n).lstrip("*").strip(), v, f"{v / total * 100:.2f}%" if total else "", NOW, party])
    return rows


def clarity_rows_from_zip(content: bytes):
    """Fallback: detailxml.zip -> rows  (<Contest text=...> containing <Choice text=... totalVotes=...>)."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        xml_name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(z.read(xml_name))
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party"]]
    for contest in root.iter():
        if strip_ns(contest.tag) != "Contest":
            continue
        title = clean(contest.attrib.get("text") or contest.attrib.get("name") or "")
        choices = [ch for ch in contest if strip_ns(ch.tag) == "Choice"]
        vlist = [to_int(ch.attrib.get("totalVotes")) or 0 for ch in choices]
        total = sum(vlist)
        for ch, v in zip(choices, vlist):
            rows.append([title, clean(ch.attrib.get("text", "")).lstrip("*").strip(), v,
                         f"{v / total * 100:.2f}%" if total else "", NOW, ch.attrib.get("party", "")])
    return rows


def plain_getter(url: str) -> bytes:
    """Default Clarity downloader: a normal HTTP request (2 tries). Raises if the server refuses (e.g. HTTP 202)."""
    return body_bytes(http_get(url, timeout=30, retries=2))


# JavaScript that runs INSIDE the browser page: downloads a URL with the browser's own network stack
# (so it carries the cookie the bot-protection gave the browser) and hands the bytes back as base64.
BROWSER_FETCH_JS = """
async (url) => {
  const r = await fetch(url, {credentials: 'include', cache: 'no-store'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  }
  return {status: r.status, waf: r.headers.get('x-amzn-waf-action') || '', b64: btoa(bin)};
}
"""


class BrowserGetter:
    """Callable  url -> bytes  that downloads through a real headless Chromium.

    1. The first call opens `warm_url` (the county's normal results page). Amazon's bot protection challenges
       the browser, the browser solves it automatically and receives a cookie.
    2. Every download then runs as an in-page fetch() carrying that cookie.
    While the challenge is still being solved the page is reloading, so a download can fail or return 202;
    we simply try again every few seconds (up to ~24 s) before giving up."""

    def __init__(self, context, warm_url: str, tries: int = 8, delay: float = 3.0):
        self.context, self.warm_url, self.tries, self.delay = context, warm_url, tries, delay
        self.page = None

    def __call__(self, url: str) -> bytes:
        if self.page is None:
            self.page = self.context.new_page()
            self.page.goto(self.warm_url, wait_until="domcontentloaded", timeout=60000)
        last = "no attempt made"
        for _ in range(self.tries):
            try:
                res = self.page.evaluate(BROWSER_FETCH_JS, url)
                if res["status"] == 200:
                    return maybe_gunzip(base64.b64decode(res["b64"]))
                last = f"HTTP {res['status']}" + (f" (waf-action={res['waf']})" if res["waf"] else "")
            except Exception as e:  # e.g. "Execution context was destroyed" while the page reloads
                last = f"{type(e).__name__}: {clean(str(e))[:100]}"
            time.sleep(self.delay)
        raise RuntimeError(f"{url} via browser: {last}")

    def close(self):
        if self.page is not None:
            self.page.close()
            self.page = None


def fetch_clarity(county_slug: str, election_id: str, getter=None):
    """Download one Clarity election and return rows.  `getter` is plain_getter unless a BrowserGetter is passed."""
    get = getter or plain_getter
    root = f"{CLARITY_ROOT}/{county_slug}/{election_id}"
    ver = get(f"{root}/current_ver.txt").decode("utf-8", "ignore").strip()
    if not re.fullmatch(r"\d+", ver):
        raise RuntimeError(f"{root}/current_ver.txt did not return a version number: {ver[:80]!r}")
    base = f"{root}/{ver}"

    errors = []
    for path in ("json/en/summary.json", "json/summary.json"):
        try:
            data = json.loads(get(f"{base}/{path}").decode("utf-8-sig"))
            rows = clarity_rows_from_json(data)
            if len(rows) > 1:
                return rows
            errors.append(f"{path}: loaded but parsed 0 rows (type={type(data).__name__}, sample={json.dumps(data)[:300]})")
        except Exception as e:
            errors.append(f"{path}: {e}")
    try:
        rows = clarity_rows_from_zip(get(f"{base}/reports/detailxml.zip"))
        if len(rows) > 1:
            return rows
        errors.append("reports/detailxml.zip: loaded but parsed 0 rows")
    except Exception as e:
        errors.append(f"reports/detailxml.zip: {e}")
    raise RuntimeError(f"Clarity [{county_slug} {election_id} v{ver}] " + " | ".join(errors))


# =====================================================================================
# 6. Statewide results from the California Secretary of State  ->  RAW_Statewide
#    Uses the SOS's media feed (media.sos.ca.gov), which is meant for scripts, and falls back to api.sos.ca.gov.
#    Each feed file holds, for one contest family, a list of "sections": one for the whole race
#    ("Governor - Statewide Results", "U.S. House of Representatives District 4 - Districtwide Results") followed by
#    one per county. We keep ONLY the whole-race sections.
#    Columns A-E match the other RAW tabs.  "Key" is a short lookup code ("CD 4", "SD 2", "AD 12", "Prop 40").
#    "SOS As Of" is the time the SOS stamped on that contest - it can differ between contest families.
# =====================================================================================

def sos_get(media_name: str, api_path: str):
    errors = []
    for url in (f"{SOS_MEDIA}/{media_name}.json", f"{SOS_API}/{api_path}"):
        try:
            return json_from(http_get(url, timeout=60))
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError(" | ".join(errors))


def sos_sections(data):
    """Normalize the two shapes the feeds come in into a plain list of sections."""
    if isinstance(data, dict):
        if isinstance(data.get("ballot-measures"), list):
            return [{"raceTitle": "Ballot Measures - Statewide Results",
                     "Reporting": data.get("Reporting", ""), "ReportingTime": data.get("ReportingTime", ""),
                     "ballot-measures": data["ballot-measures"]}]
        for v in data.values():
            if isinstance(v, list):
                return v
        return []
    return data if isinstance(data, list) else []


def parse_sos_time(s: str) -> str:
    """'November 14, 2026, 5:16 p.m.' -> '2026-11-14 17:16' (so Google Sheets treats it as a date-time)."""
    s2 = clean(s).replace("a.m.", "AM").replace("p.m.", "PM")
    for fmt in ("%B %d, %Y, %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(s2, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return clean(s)


def sos_key(contest: str) -> str:
    """Short lookup key that matches the labels in your ALL tab: 'CD 4', 'SD 2', 'AD 12', 'Prop 40'."""
    # The SOS's own names: "United States Representative District 4", "State Senate District 2",
    # "State Assembly Member District 12", "Board of Equalization Member District 1", and our "Proposition 4".
    for pat, tag in ((r"^(?:U\.?S\.?|United States)\s+(?:House|Rep\w*)[^\d]*(\d+)$", "CD"),
                     (r"^State\s+Senat\w*[^\d]*(\d+)$", "SD"),
                     (r"^State\s+Assembl\w*[^\d]*(\d+)$", "AD"),
                     (r"^Board of Equalization[^\d]*(\d+)$", "BOE"),
                     (r"^Proposition\s+(\d+)$", "Prop")):
        m = re.match(pat, contest, flags=re.I)
        if m:
            return f"{tag} {int(m.group(1))}"
    return contest


# contest label -> {county: total votes in that county}. Filled by fetch_statewide(), used by add_left_columns().
SOS_COUNTY_VOTES = {}


def sos_county_votes(sections):
    """The per-county slices of each race (the ones sos_rows() skips): {contest: {county: votes}}."""
    out = {}
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        m = re.match(r"^(?P<c>.+?)\s+-\s+(?P<county>.+?)(?:\s+County)?\s+Results$", clean(sec.get("raceTitle", "")))
        if not m or m.group("county").lower() in ("statewide", "districtwide"):
            continue
        base, county = m.group("c"), m.group("county")
        for meas in sec.get("ballot-measures") or []:
            num = re.sub(r"^0+", "", str(meas.get("Number", ""))) or "0"
            v = (to_int(meas.get("yesVotes")) or 0) + (to_int(meas.get("noVotes")) or 0)
            out.setdefault(f"Proposition {num}", {})[county] = v
        cands = sec.get("candidates") or []
        if cands:
            out.setdefault(base, {})[county] = sum(to_int(c.get("Votes")) or 0 for c in cands)
    return out


def sos_rows(sections):
    out = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        m = re.match(r"^(?P<c>.+?)\s+-\s+(?:Statewide|Districtwide) Results$", clean(sec.get("raceTitle", "")))
        if not m:
            continue  # a county slice of a race: skip
        base = m.group("c")
        reporting = clean(sec.get("Reporting", ""))
        as_of = parse_sos_time(sec.get("ReportingTime", ""))

        for meas in sec.get("ballot-measures") or []:  # propositions: one Yes row + one No row each
            num = re.sub(r"^0+", "", str(meas.get("Number", ""))) or "0"
            contest = f"Proposition {num}"
            for side in ("Yes", "No"):
                v = to_int(meas.get(f"{side.lower()}Votes"))
                out.append([contest, side, "" if v is None else v, pct_text(meas.get(f"{side.lower()}Percent")),
                            NOW, "", sos_key(contest), reporting, as_of, clean(meas.get("Name", ""))])

        for cand in sec.get("candidates") or []:
            name, party = clean(cand.get("Name", "")), clean(cand.get("Party", "") or "")
            if "Votes" in cand:
                v = to_int(cand.get("Votes"))
                out.append([base, name, "" if v is None else v, pct_text(cand.get("Percent")),
                            NOW, party, sos_key(base), reporting, as_of, ""])
            elif "yesVotes" in cand or "noVotes" in cand:  # retention-style entries (judges)
                for side in ("Yes", "No"):
                    v = to_int(cand.get(f"{side.lower()}Votes"))
                    out.append([base, f"{name} - {side}", "" if v is None else v, pct_text(cand.get(f"{side.lower()}Percent")),
                                NOW, party, sos_key(base), reporting, as_of, ""])
    return out


def fetch_statewide():
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party", "Key", "Reporting", "SOS As Of", "Description"]]
    for media_name, api_path, required in SOS_FILES:
        try:
            sections = sos_sections(sos_get(media_name, api_path))
            part = sos_rows(sections)
            SOS_COUNTY_VOTES.update(sos_county_votes(sections))
            if not part:
                raise RuntimeError("loaded but contained no whole-race results")
            rows.extend(part)
        except Exception as e:
            if required:
                raise RuntimeError(f"required SOS file '{media_name}' failed: {e}")
            print(f"[WARN] optional SOS file '{media_name}' skipped: {e}")
    return rows


# =====================================================================================
# 7. Ranked-choice (RCV) tabs for Alameda County - one tab per race, round by round
#    Same parser as before, now run once per entry in RCV_ALAMEDA. Columns A-F are exactly what your original
#    RCV_Alameda tab had; Report Time / Status / Seats were added on the right.
#    (Alameda's report is a SQL Server Reporting Services XML export - hence the odd names Tablix5, Textbox86...)
# =====================================================================================

def alameda_rcv_url(race: str) -> str:
    return f"https://www.alamedacountyca.gov/rovresults/rcv/{ALAMEDA_ELECTION}/{race}/RcvDetailedReport.xml"


def fetch_alameda_rcv(xml_url: str):
    resp = http_get(xml_url, timeout=20)
    root = ET.fromstring(body_bytes(resp))

    # Contest-level facts live on the "Tablix2" element.
    contest_title, report_time, status, seats = "Alameda RCV", "", "", ""
    for elem in root.iter():
        if strip_ns(elem.tag) == "Tablix2" and "Textbox24" in elem.attrib:
            contest_title = elem.attrib["Textbox24"]
            report_time = elem.attrib.get("timeStamp", "").replace("T", " ")[:16]
            status = elem.attrib.get("state", "")                       # e.g. "All Positions Filled"
            ms = re.search(r"(\d+)", elem.attrib.get("Textbox53", ""))  # "Number of positions to elect is 2."
            seats = int(ms.group(1)) if ms else ""
            break

    rows = [["Contest", "Round", "Candidate", "Votes", "Pct", "Source URL", "Report Time", "Status", "Seats"]]
    current_round = ""

    # Document order matters: a "Tablix5" element names the round ("Round 2"); the "choiceGroup" elements that
    # follow it are that round's candidates (plus "Blanks", "Exhausted", "Overvotes" pseudo-rows).
    for elem in root.iter():
        tag = strip_ns(elem.tag)
        if tag == "Tablix5" and "Textbox86" in elem.attrib:
            current_round = elem.attrib["Textbox86"]
        elif tag == "choiceGroup" and current_round:
            cand_name = elem.attrib.get("choiceName2", "")
            raw_votes = elem.attrib.get("votes", "0")
            raw_pct = elem.attrib.get("Textbox17", "")
            try:
                v = float(raw_votes)
            except ValueError:
                v = 0.0
            votes = int(v) if v.is_integer() else round(v, 2)  # multi-seat contests can have fractional votes
            pct = f"{float(raw_pct) * 100:.2f}%" if raw_pct.replace(".", "").isdigit() else ""
            if cand_name:
                rows.append([contest_title, current_round, cand_name, votes, pct, xml_url, report_time, status, seats])

    return rows


def discover_alameda_rcv():
    """Logs every RCV race Alameda currently lists, so you can see what else you could add to RCV_ALAMEDA."""
    soup = BeautifulSoup(http_get(f"https://www.alamedacountyca.gov/rovresults/{ALAMEDA_ELECTION}/", timeout=20).text, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        m = re.search(r"rcvresults\.htm\?race=([^&#]+)", a["href"])
        if m and unquote(m.group(1)) not in found:
            found.append(unquote(m.group(1)))
    unused = [r for r in found if r not in RCV_ALAMEDA.values()]
    print(f"[INFO] Alameda lists {len(found)} RCV races; not in RCV_ALAMEDA yet: {unused}")


# =====================================================================================
# 8. "How many ballots are left?"  (Secretary of State Unprocessed Ballot Status report)  ->  RAW_Unprocessed
#
#    Counties send the SOS their own estimates (ballots processed so far, ballots still to count, when they last
#    reported, when they expect to report next). The SOS shows them on a web page - no data feed - so a browser reads it.
#    From it we work out, per county:   left = remaining / (processed + remaining)
#    and attach it to every RAW_ tab of that county as an "Est. % Left" column, plus the county's "last reported" time
#    as "Source Updated" (when the county did not give us its own time). Your ALL tab picks both up through RESULTS.
#
#    How good is it?  It is the counties' own estimate, rolled up by the state - not AP's number (AP's is proprietary and
#    contest-specific). For a local race it is the whole county's figure, which is a reasonable guide, not an exact one.
#    Statewide and district races are averaged over their counties, weighting each by its votes in that race.
# =====================================================================================

UBS_DATA = {}   # county (lower case) -> {"county", "processed", "remaining", "cure", "last", "next"}; filled by fetch_unprocessed()


def _ubs_num(s) -> int:
    s = clean(s).replace(",", "")
    return int(float(s)) if re.fullmatch(r"-?\d+(\.\d+)?", s) else 0


def _ubs_time(s) -> str:
    """'11/16/26 6:56 PM' -> '2026-11-16 18:56'; anything else ('N/A', 'TBD') is kept as text."""
    s = clean(s)
    for fmt in ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return s


def parse_ubs_html(html: str) -> dict:
    """The SOS table -> {county_lower: record}. Columns are found by their header text, so a reordered table still works."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for tbl in soup.find_all("table"):
        trs = tbl.find_all("tr")
        header, h_idx = None, None
        for i, tr in enumerate(trs):
            cells = [clean(c.get_text(" ", strip=True)).lower() for c in tr.find_all(["th", "td"])]
            if "county" in cells and any("estimated total ballots remaining" in c for c in cells):
                header, h_idx = cells, i
                break
        if header is None:
            continue

        def find(pred):
            return next((i for i, c in enumerate(header) if pred(c)), None)
        ci = header.index("county")
        pi = find(lambda c: c.startswith("cumulative total"))
        ri = find(lambda c: "estimated total ballots remaining" in c)
        ki = find(lambda c: "left to cure" in c)
        li = find(lambda c: c == "last report")
        ni = find(lambda c: c.startswith("next results"))
        for tr in trs[h_idx + 1:]:
            cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) <= max(i for i in (ci, ri, pi) if i is not None):
                continue
            name = cells[ci]
            if not name or re.search(r"total|statewide", name, re.I):
                continue
            rec = {"county": name,
                   "processed": _ubs_num(cells[pi]) if pi is not None else 0,
                   "remaining": _ubs_num(cells[ri]),
                   "cure": _ubs_num(cells[ki]) if ki is not None and ki < len(cells) else 0,
                   "last": _ubs_time(cells[li]) if li is not None and li < len(cells) else "",
                   "next": _ubs_time(cells[ni]) if ni is not None and ni < len(cells) else ""}
            old = out.get(name.lower())
            if old is None or rec["last"] >= old["last"]:
                out[name.lower()] = rec
    if not out:
        raise RuntimeError("no Unprocessed Ballots table found on the page (blocked, or the layout changed). Page text began: "
                           + clean(soup.get_text())[:150])
    return out


def left_fraction(rec: dict) -> float:
    """Share of the ballots this county will end up counting that are still uncounted (0.0 - 1.0)."""
    remaining = rec["remaining"] + (rec["cure"] if INCLUDE_CURE_IN_LEFT else 0)
    total = rec["processed"] + remaining
    return remaining / total if total > 0 else 0.0


def fetch_unprocessed(ctx):
    """Load the SOS page in a browser context, parse the table, fill UBS_DATA, return the tab's rows."""
    page = ctx.new_page()
    try:
        page.goto(UBS_URL, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector("table tbody tr", timeout=30000)
        except Exception:
            pass  # parse whatever did render; parse_ubs_html explains if it is unusable
        html = page.content()
    finally:
        page.close()
    by_county = parse_ubs_html(html)
    UBS_DATA.clear()
    UBS_DATA.update(by_county)
    stamps = sorted(r["last"] for r in by_county.values() if re.match(r"\d{4}-", r["last"]))
    SOURCE_NOTES["RAW_Unprocessed"] = f"{len(by_county)} counties" + (f"; reports from {stamps[0]} to {stamps[-1]}" if stamps else "")
    rows = [["County", "Processed", "Remaining", "To Cure", "Est. % Left", "Last Report", "Next Results", "Updated"]]
    for key in sorted(by_county):
        r = by_county[key]
        rows.append([r["county"], r["processed"], r["remaining"], r["cure"], f"{left_fraction(r) * 100:.2f}%", r["last"], r["next"], NOW])
    return rows


def counties_for_tab(tab: str):
    if tab in SOURCE_COUNTIES:
        return SOURCE_COUNTIES[tab]
    return ["Alameda"] if tab.startswith("RCV_") else None


def weighted_left(votes_by_county: dict, ubs: dict):
    """Left-to-count share for a race spread over several counties.
    County c has counted V_c votes in the race and has left-fraction r_c, so it will end up with V_c / (1 - r_c) votes,
    of which V_c * r_c / (1 - r_c) are still to come. Add those up across the counties. None if no county is known."""
    num = den = 0.0
    for county, v in votes_by_county.items():
        rec = ubs.get(county.lower())
        if not rec or not v:
            continue
        r = left_fraction(rec)
        if r >= 1:
            continue
        num += v * r / (1 - r)
        den += v / (1 - r)
    return num / den if den else None


def add_left_columns(tabs: dict, ubs: dict):
    """Append "Est. % Left" (and "Source Updated" when the tab has no timestamp of its own) to every tab we can place.
    Only columns are APPENDED on the right, so every existing cell address stays where it was."""
    for tab, rows in tabs.items():
        header = rows[0]
        if "Est. % Left" in header:
            continue
        if tab == "RAW_Statewide":
            ci = header.index("Contest")
            cache = {}
            header.append("Est. % Left")
            for row in rows[1:]:
                c = row[ci]
                if c not in cache:
                    v = weighted_left(SOS_COUNTY_VOTES.get(c, {}), ubs)
                    cache[c] = "" if v is None else f"{v * 100:.2f}%"
                row.append(cache[c])
            continue
        counties = counties_for_tab(tab)
        recs = [ubs[c.lower()] for c in (counties or []) if c.lower() in ubs]
        if not recs:
            continue
        weight = sum(r["processed"] + r["remaining"] for r in recs) or 1
        frac = sum(left_fraction(r) * (r["processed"] + r["remaining"]) for r in recs) / weight
        stamps = [r["last"] for r in recs if re.match(r"\d{4}-", r["last"])]
        add_time = not any(h in header for h in ("Source Updated", "SOS As Of", "Report Time"))
        # A tab that has a "Source Updated" column but could not fill it (e.g. its XML feed was unavailable) borrows the
        # county's "last reported" time instead.
        if not add_time and "Source Updated" in header and stamps:
            si = header.index("Source Updated")
            if not any(row[si] for row in rows[1:]):
                for row in rows[1:]:
                    row[si] = max(stamps)
        header.append("Est. % Left")
        if add_time:
            header.append("Source Updated")
        for row in rows[1:]:
            row.append(f"{frac * 100:.2f}%")
            if add_time:
                row.append(max(stamps) if stamps else "")


# =====================================================================================
# RUNNER
# =====================================================================================

def run_source(tabs, status, tab_name, fetcher, note=""):
    """Run one fetcher. Success -> its rows go into `tabs`. Failure or zero rows -> nothing is written for that
    tab (it keeps its previous data) and the reason is recorded in `status` (the STATUS tab).
    `status` is a dict keyed by tab name, so a second attempt for the same tab simply replaces the first."""
    try:
        rows = fetcher()
        note = note or SOURCE_NOTES.pop(tab_name, "")
        n = len(rows) - 1
        if n <= 0:
            print(f"[EMPTY] {tab_name}: parsed 0 rows - leaving the tab's previous data alone")
            status[tab_name] = [tab_name, "EMPTY (previous data kept)", 0, "Page loaded but no results were parsed", NOW_PT]
            return
        tabs[tab_name] = normalize(rows)
        print(f"[OK] {tab_name}: fetched {n} rows" + (f" ({note})" if note else ""))
        status[tab_name] = [tab_name, "OK", n, note, NOW_PT]
    except Exception as e:
        print(f"[ERROR] {tab_name} failed: {e}")
        status[tab_name] = [tab_name, "ERROR (previous data kept)", "", str(e)[:300], NOW_PT]


def main():
    tabs, status = {}, {}

    # ---- Stage 1: everything that works with plain HTTP requests -----------------------------------------
    sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RAW_Alameda": fetch_alameda_main,
        "RAW_LA": fetch_la,
        "RAW_SF": fetch_sf,
        "RAW_Statewide": fetch_statewide,
    }
    for tab, (county, election_id) in CLARITY_SOURCES.items():
        sources[tab] = lambda c=county, e=election_id: fetch_clarity(c, e)          # plain request first
    for tab, url in LVT_CLEAN_SOURCES.items():
        sources[tab] = lambda u=url, t=tab: fetch_lvt_clean(u, t)
    for tab, url in LVT_XML_SOURCES.items():
        sources[tab] = lambda u=url: fetch_lvt_xml(u)
    for tab, race in RCV_ALAMEDA.items():
        sources[tab] = lambda r=race: fetch_alameda_rcv(alameda_rcv_url(r))

    for tab, fetcher in sources.items():
        run_source(tabs, status, tab, fetcher)

    try:
        discover_alameda_rcv()
    except Exception as e:
        print(f"[INFO] RCV discovery skipped: {e}")

    # ---- Stage 2: things that need a real browser ---------------------------------------------------------
    #   (a) Clarity counties that were refused in stage 1   (b) the Secretary of State's "ballots left" page
    #   (c) Sacramento, which is a JavaScript page.
    clarity_blocked = [t for t in CLARITY_SOURCES if t not in tabs]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()  # Sacramento's page - set up exactly as before
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")  # a normal-looking browser for the rest

            for tab in clarity_blocked:  # (a)
                county, election_id = CLARITY_SOURCES[tab]
                plain_error = status[tab][3]
                getter = BrowserGetter(ctx, f"{CLARITY_ROOT}/{county}/{election_id}/")
                run_source(tabs, status, tab, lambda c=county, e=election_id, g=getter: fetch_clarity(c, e, g),
                           note="fetched through the browser (a plain request was refused)")
                getter.close()
                if tab not in tabs:  # still failing: keep both explanations
                    status[tab][3] = f"plain: {plain_error} || browser: {status[tab][3]}"[:300]

            run_source(tabs, status, "RAW_Unprocessed", lambda: fetch_unprocessed(ctx))  # (b)
            run_source(tabs, status, "RAW_Sacramento", lambda: fetch_sacramento_web(page))  # (c)
            browser.close()
    except Exception as e:
        print(f"[ERROR] browser stage failed: {e}")
        for t in ("RAW_Unprocessed", "RAW_Sacramento"):
            if t not in status:
                status[t] = [t, "ERROR (previous data kept)", "", str(e)[:300], NOW_PT]

    # Attach "Est. % Left" and the county's "last reported" time to every tab we can place (see section 8).
    # If the ballots-left page could not be read, the tabs simply go without those columns this run.
    try:
        if UBS_DATA:
            add_left_columns(tabs, UBS_DATA)
    except Exception as e:
        print(f"[ERROR] adding the ballots-left columns failed: {e}")
        status["RAW_Unprocessed"] = ["RAW_Unprocessed", "ERROR (columns not added)", "", str(e)[:300], NOW_PT]

    # ---- Stage 3: send everything to Google Sheets --------------------------------------------------------
    # STATUS tab: one row per source, so you can see at a glance which tabs are stale and why.
    tabs["STATUS"] = normalize([["Tab", "Status", "Rows", "Detail", "Run time (Pacific)"]] + list(status.values()))

    if len(tabs) == 1:
        print("Nothing was fetched - not calling the Google Sheet.")
        sys.exit(1)

    res = requests.post(WEBHOOK_URL, json={"secret": SECRET_TOKEN, "tabs": tabs}, timeout=90)
    print("Google Sheet response:", res.status_code, res.text[:200])
    # The Apps Script answers "Success" (optionally followed by a note). Anything else = the sheet rejected the data.
    if res.status_code != 200 or not res.text.strip().startswith("Success"):
        sys.exit(1)  # make the GitHub run turn red


if __name__ == "__main__":
    main()
