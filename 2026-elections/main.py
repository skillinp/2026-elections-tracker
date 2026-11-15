import os
import re
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime
from playwright.sync_api import sync_playwright

WEBHOOK_URL = os.environ["APPS_SCRIPT_URL"]
SECRET_TOKEN = os.environ["WEBHOOK_SECRET"]
NOW = datetime.now().strftime("%Y-%m-%d %H:%M")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, application/xml, text/plain, */*",
}

def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag

# 1. WORKING: Santa Barbara
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

# 2. WORKING: Alameda RCV
def fetch_alameda_rcv(xml_url: str):
    resp = requests.get(xml_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    contest_title = "Alameda RCV"
    for elem in root.iter():
        if strip_ns(elem.tag) == "Tablix2" and "Textbox24" in elem.attrib:
            contest_title = elem.attrib["Textbox24"]
            break

    rows = [["Contest", "Round", "Candidate", "Votes", "Pct", "Source URL"]]
    current_round = ""

    for elem in root.iter():
        tag = strip_ns(elem.tag)
        if tag == "Tablix5" and "Textbox86" in elem.attrib:
            current_round = elem.attrib["Textbox86"]
        elif tag == "choiceGroup" and current_round:
            cand_name = elem.attrib.get("choiceName2", "")
            raw_votes = elem.attrib.get("votes", "0")
            raw_pct = elem.attrib.get("Textbox17", "")
            votes = round(float(raw_votes)) if raw_votes.replace(".", "").isdigit() else 0
            pct = f"{float(raw_pct) * 100:.2f}%" if raw_pct.replace(".", "").isdigit() else ""
            if cand_name:
                rows.append([contest_title, current_round, cand_name, votes, pct, xml_url])

    return rows

# 3. EXPANDED: Alameda Main (captures ALL panels, including measures and local races)
def fetch_alameda_main():
    url = "https://www.alamedacountyca.gov/rovresults/260/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for panel in soup.select("div.panelDtl"):
        # Find the button or heading immediately preceding this panelDtl
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

# 4. WORKING: Sacramento Web
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

# 5. FIXED: Los Angeles (fetches GetElectionData + GetCounterData?electionID=4348)
def fetch_la():
    base = "https://results.lavote.gov/ElectionResults"
    hdrs = {**HEADERS, "Referer": "https://results.lavote.gov/"}

    meta = requests.get(f"{base}/GetElectionData?electionID=4348", headers=hdrs, timeout=20).json().get("Data", {})
    counter_raw = requests.get(f"{base}/GetCounterData?electionID=4348", headers=hdrs, timeout=20).json()
    counter_data = counter_raw.get("Data", counter_raw)
    print("LA GetCounterData sample:", str(counter_data)[:350])

    # Index all candidate tallies in GetCounterData by candidate ID
    live_votes = {}
    def walk(obj):
        if isinstance(obj, dict):
            cid = obj.get("ID") or obj.get("CandidateID") or obj.get("CandID") or obj.get("Id")
            # Grab any integer vote field (excluding ID/Order fields)
            vote_val = next(
                (obj[k] for k in ["Votes", "TotalVotes", "VoteCount", "CandidateVotes", "Count", "Ballots"]
                 if isinstance(obj.get(k), (int, float))),
                None
            )
            if cid is not None and vote_val is not None:
                live_votes[cid] = {
                    "votes": int(vote_val),
                    "pct": obj.get("Percent") or obj.get("Percentage") or obj.get("VotePercent") or ""
                }
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
    walk(counter_data)

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
                tally = live_votes.get(cid, {})
                votes = tally.get("votes", cand.get("Votes") or 0)
                pct = tally.get("pct", "")
                resolved.append((cand.get("Name") or "", int(votes), pct))

            total_votes = sum(v for _, v, _ in resolved)
            for name, votes, pct in resolved:
                if not pct and total_votes > 0:
                    pct = f"{(votes / total_votes) * 100:.2f}%"
                rows.append([c_title, name, votes, pct, NOW])
    return rows

# 6. FIXED: San Francisco via Playwright
def fetch_sf_browser(page):
    page.goto("https://www.sfelections.org/results/20261103w/index.html", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2500)
    soup = BeautifulSoup(page.content(), "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for table in soup.find_all("table"):
        first_th = table.find("th")
        prev_h = table.find_previous(["h2", "h3", "h4", "caption"])
        contest = (first_th.get_text(" ", strip=True) if first_th and first_th.get("colspan") else (prev_h.get_text(" ", strip=True) if prev_h else ""))
        if not contest:
            continue
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 3 and cells[0] != contest:
                cand = cells[0]
                if cand.lower() in ("candidate", "total", "total votes", "undervotes", "overvotes", "registered voters", "ballots cast"):
                    continue
                nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                pcts = [c for c in cells[1:] if "%" in c]
                if nums:
                    rows.append([contest, cand, int(nums[0]), pcts[0] if pcts else "", NOW])

    if len(rows) == 1:
        print("SF rendered body sample:", re.sub(r"\s+", " ", soup.body.get_text())[:400] if soup.body else "No body")
    return rows

# 7. FIXED: livevoterturnout.com (auto-resolves live election URL & expands all contests)
def fetch_livevoterturnout(page, slug: str):
    # Visiting /ENR/<slug>/ redirects to the active election ID automatically
    page.goto(f"https://www.livevoterturnout.com/ENR/{slug}/", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)
    print(f"livevoterturnout [{slug}] resolved URL:", page.url)

    # Click "All Contests" or expand buttons if present so all races render
    for selector in ["text=All Contests", "text=Show All", "button:has-text('All')"]:
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click(timeout=2000)
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass

    soup = BeautifulSoup(page.content(), "html.parser")
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]

    for tbl in soup.find_all("table"):
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
                    rows.append([contest, cells[0], int(nums[-1]), pcts[-1] if pcts else "", NOW])

    return rows

if __name__ == "__main__":
    tabs = {}

    static_sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_Alameda": fetch_alameda_main,
        "RAW_LA": fetch_la,
    }

    for tab_name, fetcher in static_sources.items():
        try:
            rows = fetcher()
            tabs[tab_name] = rows
            print(f"[OK] {tab_name}: fetched {len(rows) - 1} rows")
        except Exception as e:
            print(f"[ERROR] {tab_name} failed: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        browser_sources = {
            "RAW_Sacramento": lambda: fetch_sacramento_web(page),
            "RAW_SF": lambda: fetch_sf_browser(page),
            "RAW_SanDiego": lambda: fetch_livevoterturnout(page, "sandiegocaenr"),
            "RAW_Riverside": lambda: fetch_livevoterturnout(page, "riversidecaenr"),
            "RAW_SanBernardino": lambda: fetch_livevoterturnout(page, "sanbernardinocaenr"),
            "RAW_Imperial": lambda: fetch_livevoterturnout(page, "imperialcaenr"),
        }

        for tab_name, fetcher in browser_sources.items():
            try:
                rows = fetcher()
                tabs[tab_name] = rows
                print(f"[OK] {tab_name}: fetched {len(rows) - 1} rows")
            except Exception as e:
                print(f"[ERROR] {tab_name} failed: {e}")

        browser.close()

    if tabs:
        res = requests.post(
            WEBHOOK_URL,
            json={"secret": SECRET_TOKEN, "tabs": tabs},
            timeout=30
        )
        print("Google Sheet response:", res.status_code, res.text)
