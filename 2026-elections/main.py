"""
2026 California election results -> Google Sheets (through your Apps Script webhook).

Run by .github/workflows/update_elections.yml every 30 minutes.

WHERE TO ADD THINGS (all three are one-line edits in the CONFIG block below)
  * another ranked-choice (RCV) tab ............ add a line to RCV_ALAMEDA
  * another Clarity county (like Butte) ........ add a line to CLARITY_SOURCES
  * another livevoterturnout.com county ........ add a line to LVT_CLEAN_SOURCES
The Apps Script creates the tab automatically the first time it receives it.

SAFETY NET: if a source fails (or parses to zero rows) its tab is simply NOT overwritten,
so you keep the last good data, and the reason is written to the STATUS tab.
"""
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

WEBHOOK_URL = os.environ["APPS_SCRIPT_URL"]
SECRET_TOKEN = os.environ["WEBHOOK_SECRET"]

# NOTE: GitHub runners use UTC, so this "Updated" stamp is UTC (unchanged from your version).
# To stamp Pacific time instead, use:  datetime.now(ZoneInfo("America/Los_Angeles")).strftime(...)
NOW = datetime.now().strftime("%Y-%m-%d %H:%M")
NOW_PT = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")  # used on STATUS tab

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xml, text/plain, */*",
}

# =====================================================================================
# CONFIG - the parts you will edit during the election
# =====================================================================================

# Ranked-choice races in Alameda County (Oakland / Berkeley / Albany).
#   key   = the Google Sheet tab name
#   value = the race code: the text after "race=" in the county's "RCV tabulations" link
#           (https://www.alamedacountyca.gov/rovresults/260/), with %2F turned into "/"
ALAMEDA_ELECTION = 260  # the "260" in .../rovresults/260/
RCV_ALAMEDA = {
    "RCV_Alameda":          "Oakland/008-SchoolDirector_District_6",   # your existing tab
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

# Counties on Clarity Elections (results.enr.clarityelections.com/CA/<County>/<id>/).
#   tab name -> (County path segment exactly as it appears in the URL, Clarity election id)
CLARITY_SOURCES = {
    "RAW_Butte": ("Butte", "126778"),   # id confirmed on buttevotes.ca.gov's Nov 3, 2026 page
}

# Counties on livevoterturnout.com (same vendor as San Diego), with names cleaned up.
#   tab name -> Index page URL
LVT_CLEAN_SOURCES = {
    "RAW_Riverside":     "https://www.livevoterturnout.com/ENR/riversidecaenr/12/en/Index_12.html",
    "RAW_SanBernardino": "https://www.livevoterturnout.com/ENR/sanbernardinocaenr/3/en/Index_3.html",
    "RAW_Imperial":      "https://www.livevoterturnout.com/ENR/imperialcaenr/6/en/Index_6.html",
}

SF_RESULTS_URL = "https://www.sfelections.org/results/20261103/index.html"

# California Secretary of State statewide results.
# (file on media.sos.ca.gov, matching api.sos.ca.gov path, required?)
# "required" files must all load or the tab is left untouched; optional ones just log a warning.
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
    ("supreme-court",                        "supreme-court",                       False),
    ("court-of-appeal",                      "courts-of-appeal/district/all",       False),
]

# =====================================================================================
# SMALL HELPERS
# =====================================================================================

def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def clean(s) -> str:
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
    """'65.6' -> '65.6%' (Google Sheets then stores it as a real percentage, like your other tabs)."""
    s = clean(str(x)) if x is not None else ""
    if not s:
        return ""
    return s if s.endswith("%") else f"{s}%"


def http_get(url, *, retries=3, timeout=30, headers=None):
    """GET with retries on 5xx / 429 / network errors. Raises RuntimeError with a useful message."""
    last = "no attempt made"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code} from {url} (server={r.headers.get('server')})"
            if r.status_code < 500 and r.status_code != 429:
                break  # 4xx: retrying will not help
        except requests.RequestException as e:
            last = f"{type(e).__name__} on {url}: {e}"
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError(last)


def body_bytes(resp) -> bytes:
    """Response body, un-gzipping it ourselves if the server sent gzip WITHOUT a Content-Encoding header
    (Clarity does this for some JSON files; requests then hands json() raw gzip bytes and fails with
    'Expecting value: line 1 column 1 (char 0)')."""
    b = resp.content
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b


def json_from(resp):
    return json.loads(body_bytes(resp).decode("utf-8-sig"))


def normalize(rows):
    """Apps Script's setValues() needs every row to be the same width, and no None values."""
    width = max(len(r) for r in rows)
    return [[("" if v is None else v) for v in r] + [""] * (width - len(r)) for r in rows]


# =====================================================================================
# 1. EXISTING FETCHERS - unchanged from your version (the ALL tab points at their rows by cell address)
# =====================================================================================

# WORKING: Santa Barbara
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


# WORKING: Alameda Main
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


# WORKING: Sacramento Web (Playwright)
# (This tab has some junk rows from nested HTML tables, but I left it alone on purpose:
#  cleaning it up would shift the row numbers your ALL formulas point at.)
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


# WORKING: Los Angeles
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


# WORKING: livevoterturnout.com multi-page static HTML crawler (San Diego)
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
# 2. NEW: Riverside / San Bernardino / Imperial  (livevoterturnout.com, same crawler as San Diego)
#    The raw crawl doubles every name ("X (DEM) X, DEM", "YES YES"); these tabs get tidy names instead.
#    Your San Diego tab is left exactly as it was.
# =====================================================================================

def clean_lvt_contest(raw: str) -> str:
    s = clean(raw)
    s = re.sub(r"^Contest:\s*", "", s, flags=re.I)
    m = re.match(r"^(?P<t>.+?),\s*VOTE FOR (?P<n>\d+)\s+(?P=t)\s*\(VOTE FOR (?P=n)\)$", s, flags=re.I)
    if m:
        return f"{m.group('t').strip()} (Vote for {m.group('n')})"
    return s


def clean_lvt_candidate(raw: str):
    """'COREY A JACKSON (DEM) COREY A JACKSON, DEM' -> ('COREY A JACKSON', 'DEM');  'YES YES' -> ('YES', '')."""
    s = clean(raw)
    m = re.match(r"^(?P<n>.+?)\s*\((?P<p>[^()]*)\)\s*(?P=n),\s*[A-Za-z .\-]+$", s)
    if m:
        return m.group("n").strip(), m.group("p").strip()
    m = re.match(r"^(?P<n>.+?)\s+(?P=n)$", s)
    if m:
        return m.group("n").strip(), ""
    return s, ""


def fetch_lvt_clean(index_url: str):
    raw = fetch_lvt_multipage(index_url)  # the proven crawler above
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party"]]
    for contest, cand, votes, pct, updated in raw[1:]:
        name, party = clean_lvt_candidate(cand)
        rows.append([clean_lvt_contest(contest), name, votes, pct, updated, party])
    return rows


# =====================================================================================
# 3. NEW: San Francisco
#    Your old URL (detail.html) is only a menu of PDF/Excel links, which is why it parsed to 0 rows.
#    The numbers live on index.html: one <h4> per contest followed by one results table.
#    Ranked-choice contests show "Round 1" and the last round; the Round column says which one you see.
# =====================================================================================

def fetch_sf():
    resp = http_get(SF_RESULTS_URL, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    source_updated = ""
    m = re.search(r"Last Update:\s*([A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M)", soup.get_text(" ", strip=True))
    if m:
        try:
            source_updated = datetime.strptime(m.group(1), "%B %d, %Y %I:%M %p").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            source_updated = m.group(1)

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Round", "Source Updated"]]
    contest = None
    for el in soup.find_all(["h4", "table"]):
        if el.name == "h4":
            contest = clean(el.get_text(" ", strip=True))
            continue
        if not contest:
            continue  # tables before the first contest heading (turnout summary)
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

            if not is_rcv:
                votes = to_int(cells[1])
                if votes is None:
                    continue
                rows.append([contest, name, votes, cells[2] if len(cells) > 2 else "", NOW, "", source_updated])
            else:
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
# 4. NEW: Butte County (Clarity Elections)
#    Why it failed before: (a) ALL.json is not a real Clarity file - the data is json/en/summary.json;
#    (b) Clarity sometimes sends that file gzipped without a Content-Encoding header, so requests'
#    .json() choked ("Expecting value: line 1 column 1 (char 0)"). Both are handled here.
#    If the JSON route ever fails, it falls back to Clarity's detailxml.zip download.
# =====================================================================================

CLARITY_ROOT = "https://results.enr.clarityelections.com/CA"


def clarity_rows_from_json(data):
    contests = data
    if isinstance(data, dict):  # tolerate {"Contests": [...]}-style wrappers
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
            party = party if isinstance(party, str) and not re.fullmatch(r"[\d.,%\s]*", party) else ""
            rows.append([title, clean(n), v, f"{v / total * 100:.2f}%" if total else "", NOW, party])
    return rows


def clarity_rows_from_zip(content: bytes):
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
            rows.append([title, clean(ch.attrib.get("text", "")), v, f"{v / total * 100:.2f}%" if total else "", NOW, ch.attrib.get("party", "")])
    return rows


def fetch_clarity(county_slug: str, election_id: str):
    root = f"{CLARITY_ROOT}/{county_slug}/{election_id}"
    ver = http_get(f"{root}/current_ver.txt", timeout=20).text.strip()
    if not re.fullmatch(r"\d+", ver):
        raise RuntimeError(f"{root}/current_ver.txt did not return a version number: {ver[:80]!r}")
    base = f"{root}/{ver}"

    errors = []
    for path in ("json/en/summary.json", "json/summary.json"):
        try:
            data = json_from(http_get(f"{base}/{path}", timeout=30))
            rows = clarity_rows_from_json(data)
            if len(rows) > 1:
                return rows
            errors.append(f"{path}: loaded but parsed 0 rows (type={type(data).__name__}, sample={json.dumps(data)[:300]})")
        except Exception as e:
            errors.append(f"{path}: {e}")
    try:
        rows = clarity_rows_from_zip(http_get(f"{base}/reports/detailxml.zip", timeout=60).content)
        if len(rows) > 1:
            return rows
        errors.append("reports/detailxml.zip: loaded but parsed 0 rows")
    except Exception as e:
        errors.append(f"reports/detailxml.zip: {e}")
    raise RuntimeError(f"Clarity [{county_slug} {election_id} v{ver}] " + " | ".join(errors))


# =====================================================================================
# 5. NEW: Statewide results from the California Secretary of State
#    Uses the SOS's media feed (media.sos.ca.gov), which is meant for scripts, and falls back to
#    api.sos.ca.gov. Only the whole-race sections ("... - Statewide Results" / "... - Districtwide
#    Results") are kept; the per-county slices inside those files are skipped.
#    Columns A-E match your other RAW tabs, so lookups work the same way.
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
    s2 = clean(s).replace("a.m.", "AM").replace("p.m.", "PM")
    for fmt in ("%B %d, %Y, %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(s2, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return clean(s)


def sos_key(contest: str) -> str:
    """Short lookup key that matches the labels in your ALL tab: 'CD 4', 'SD 2', 'AD 12', 'Prop 40'."""
    for pat, tag in ((r"^U\.?S\.?\s+(?:House|Rep)[^\d]*(\d+)$", "CD"),
                     (r"^State\s+Senat[^\d]*(\d+)$", "SD"),
                     (r"^State\s+Assembl[^\d]*(\d+)$", "AD"),
                     (r"^Board of Equalization[^\d]*(\d+)$", "BOE"),
                     (r"^Proposition\s+(\d+)$", "Prop")):
        m = re.match(pat, contest, flags=re.I)
        if m:
            return f"{tag} {int(m.group(1))}"
    return contest


def sos_rows(sections):
    out = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        m = re.match(r"^(?P<c>.+?)\s+-\s+(?:Statewide|Districtwide) Results$", clean(sec.get("raceTitle", "")))
        if not m:
            continue  # a county slice of a race
        base = m.group("c")
        reporting = clean(sec.get("Reporting", ""))
        as_of = parse_sos_time(sec.get("ReportingTime", ""))

        for meas in sec.get("ballot-measures") or []:
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
            elif "yesVotes" in cand or "noVotes" in cand:  # retention-style entries
                for side in ("Yes", "No"):
                    v = to_int(cand.get(f"{side.lower()}Votes"))
                    out.append([base, f"{name} - {side}", "" if v is None else v, pct_text(cand.get(f"{side.lower()}Percent")),
                                NOW, party, sos_key(base), reporting, as_of, ""])
    return out


def fetch_statewide():
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated", "Party", "Key", "Reporting", "SOS As Of", "Description"]]
    for media_name, api_path, required in SOS_FILES:
        try:
            part = sos_rows(sos_sections(sos_get(media_name, api_path)))
            if not part:
                raise RuntimeError("loaded but contained no whole-race results")
            rows.extend(part)
        except Exception as e:
            if required:
                raise RuntimeError(f"required SOS file '{media_name}' failed: {e}")
            print(f"[WARN] optional SOS file '{media_name}' skipped: {e}")
    return rows


# =====================================================================================
# 6. RANKED-CHOICE (RCV) tabs for Alameda County
#    Same parser as before, now run once per entry in RCV_ALAMEDA. Extra columns on the right
#    (Report Time, Status, Seats) are new; columns A-F are exactly what your RCV_Alameda tab has today.
# =====================================================================================

def alameda_rcv_url(race: str) -> str:
    return f"https://www.alamedacountyca.gov/rovresults/rcv/{ALAMEDA_ELECTION}/{race}/RcvDetailedReport.xml"


def fetch_alameda_rcv(xml_url: str):
    resp = http_get(xml_url, timeout=20)
    root = ET.fromstring(body_bytes(resp))

    contest_title, report_time, status, seats = "Alameda RCV", "", "", ""
    for elem in root.iter():
        if strip_ns(elem.tag) == "Tablix2" and "Textbox24" in elem.attrib:
            contest_title = elem.attrib["Textbox24"]
            report_time = elem.attrib.get("timeStamp", "").replace("T", " ")[:16]
            status = elem.attrib.get("state", "")
            ms = re.search(r"(\d+)", elem.attrib.get("Textbox53", ""))
            seats = int(ms.group(1)) if ms else ""
            break

    rows = [["Contest", "Round", "Candidate", "Votes", "Pct", "Source URL", "Report Time", "Status", "Seats"]]
    current_round = ""

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
# RUNNER
# =====================================================================================

def run_source(tabs, status, tab_name, fetcher):
    try:
        rows = fetcher()
        n = len(rows) - 1
        if n <= 0:
            print(f"[EMPTY] {tab_name}: parsed 0 rows - leaving the tab's previous data alone")
            status.append([tab_name, "EMPTY (previous data kept)", 0, "Page loaded but no results were parsed", NOW_PT])
            return
        tabs[tab_name] = normalize(rows)
        print(f"[OK] {tab_name}: fetched {n} rows")
        status.append([tab_name, "OK", n, "", NOW_PT])
    except Exception as e:
        print(f"[ERROR] {tab_name} failed: {e}")
        status.append([tab_name, "ERROR (previous data kept)", "", str(e)[:300], NOW_PT])


def main():
    tabs, status = {}, []

    sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RAW_Alameda": fetch_alameda_main,
        "RAW_LA": fetch_la,
        "RAW_SF": fetch_sf,
        "RAW_SanDiego": lambda: fetch_lvt_multipage("https://www.livevoterturnout.com/ENR/sandiegocaenr/25/en/Index_25.html"),
        "RAW_Statewide": fetch_statewide,
    }
    for tab, (county, election_id) in CLARITY_SOURCES.items():
        sources[tab] = lambda c=county, e=election_id: fetch_clarity(c, e)
    for tab, url in LVT_CLEAN_SOURCES.items():
        sources[tab] = lambda u=url: fetch_lvt_clean(u)
    for tab, race in RCV_ALAMEDA.items():
        sources[tab] = lambda r=race: fetch_alameda_rcv(alameda_rcv_url(r))

    for tab, fetcher in sources.items():
        run_source(tabs, status, tab, fetcher)

    try:
        discover_alameda_rcv()
    except Exception as e:
        print(f"[INFO] RCV discovery skipped: {e}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            run_source(tabs, status, "RAW_Sacramento", lambda: fetch_sacramento_web(page))
            browser.close()
    except Exception as e:
        print(f"[ERROR] RAW_Sacramento (browser) failed: {e}")
        status.append(["RAW_Sacramento", "ERROR (previous data kept)", "", str(e)[:300], NOW_PT])

    # STATUS tab: one row per source, so you can see at a glance which tabs are stale and why.
    tabs["STATUS"] = normalize([["Tab", "Status", "Rows", "Detail", "Run time (Pacific)"]] + status)

    if len(tabs) == 1:
        print("Nothing was fetched - not calling the Google Sheet.")
        sys.exit(1)

    res = requests.post(WEBHOOK_URL, json={"secret": SECRET_TOKEN, "tabs": tabs}, timeout=60)
    print("Google Sheet response:", res.status_code, res.text[:200])
    if res.status_code != 200 or res.text.strip() != "Success":
        sys.exit(1)  # make the GitHub run turn red if the sheet did not accept the data


if __name__ == "__main__":
    main()
