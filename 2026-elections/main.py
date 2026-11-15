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

# 3. FIXED: Los Angeles (merges GetElectionData metadata with live vote totals endpoint)
def fetch_la():
    base = "https://results.lavote.gov/ElectionResults"
    hdrs = {**HEADERS, "Referer": "https://results.lavote.gov/"}

    meta = requests.get(f"{base}/GetElectionData?electionID=4348", headers=hdrs, timeout=20).json().get("Data", {})

    # Fetch live tallies from LA's results endpoint
    live_votes = {}
    for ep in ["GetElectionResults?electionID=4348", "GetResults?electionID=4348", "GetLiveResults?electionID=4348"]:
        r = requests.get(f"{base}/{ep}", headers=hdrs, timeout=20)
        if r.status_code == 200 and r.text.strip().startswith("{"):
            print(f"LA live endpoint hit: {ep} -> {r.text[:250]}")
            res_json = r.json()
            # Recursively index any dict that has an 'ID' and 'Votes'
            def walk(obj):
                if isinstance(obj, dict):
                    if "ID" in obj and "Votes" in obj:
                        live_votes[obj["ID"]] = obj
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item)
            walk(res_json)
            if live_votes:
                break

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for grp in meta.get("ContestGroups") or []:
        if not isinstance(grp, dict):
            continue
        for contest in grp.get("Contests") or []:
            if not isinstance(contest, dict):
                continue
            c_title = contest.get("Title") or contest.get("Name") or ""
            cands = contest.get("Candidates") or []
            # Calculate total contest votes so we can compute percentage cleanly
            resolved = []
            for cand in cands:
                if not isinstance(cand, dict):
                    continue
                cid = cand.get("ID")
                tally = live_votes.get(cid, cand)
                votes = tally.get("Votes") or 0
                resolved.append((cand.get("Name") or "", int(votes), tally.get("Percent") or tally.get("Percentage") or ""))

            total_contest_votes = sum(v for _, v, _ in resolved)
            for name, votes, pct in resolved:
                if not pct and total_contest_votes > 0:
                    pct = f"{(votes / total_contest_votes) * 100:.2f}%"
                rows.append([c_title, name, votes, pct, NOW])
    return rows

# 4. NEW: Alameda Main (parses <h3 class="racePanelHeading"> + <div class="panelDtl">)
def fetch_alameda_main():
    url = "https://www.alamedacountyca.gov/rovresults/260/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for btn in soup.select("button[data-target]"):
        h3 = btn.find("h3", class_="racePanelHeading")
        target_id = btn.get("data-target", "").lstrip("#")
        if not h3 or not target_id:
            continue
        contest = h3.get_text(" ", strip=True)
        panel = soup.find(id=target_id)
        if not panel:
            continue

        # Each candidate row inside panelDtl has candidate name, vote count, and percentage
        for tr in panel.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                pcts = [c for c in cells[1:] if "%" in c]
                if nums and cells[0].lower() not in ("candidate", "total", "total votes", "registered voters", "ballots cast"):
                    rows.append([contest, cells[0], int(nums[-1]), pcts[-1] if pcts else "", NOW])
    return rows

# 5. NEW: Sacramento Web (https://eresults.saccounty.net/)
def fetch_sacramento_web(page):
    page.goto("https://eresults.saccounty.net/", wait_until="networkidle", timeout=30000)
    soup = BeautifulSoup(page.content(), "html.parser")

    rows = [["Contest", "Candidate", "Party", "Votes", "Pct", "Updated"]]
    # Find each contest header containing "(Vote for" or Measure header, then read its candidate table
    for header in soup.find_all(["h2", "h3", "h4", "th", "td", "div", "span"]):
        txt = re.sub(r"\s+", " ", header.get_text(" ", strip=True))
        if "(Vote for" in txt and len(txt) < 150:
            # Find the candidate table following this header (skip the Times Cast / Undervotes summary table)
            tbl = header.find_next("table")
            while tbl and "Candidate" not in tbl.get_text():
                tbl = tbl.find_next("table")
            if not tbl:
                continue
            for tr in tbl.find_all("tr"):
                cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                cells = [c for c in cells if c]
                # Expected columns: [Candidate, Party, Total, Pct] or [Candidate, Total, Pct]
                if len(cells) >= 3 and cells[0] not in ("Candidate", "Total Votes", "Times Cast", "Undervotes", "Overvotes"):
                    nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                    pcts = [c for c in cells[1:] if "%" in c]
                    party = cells[1] if len(cells) >= 4 and not cells[1].replace(",", "").isdigit() else ""
                    if nums:
                        rows.append([txt, cells[0], party, int(nums[-1]), pcts[-1] if pcts else "", NOW])
    return rows

# 6. NEW: San Francisco + livevoterturnout (San Diego, Riverside, San Bernardino, Imperial)
def fetch_livevoterturnout(page, slug: str, election_id: str):
    url = f"https://www.livevoterturnout.com/ENR/{slug}/{election_id}/en/Index_{election_id}.html"
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2500)
    soup = BeautifulSoup(page.content(), "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for card in soup.select(".panel, .card, .contest-container, table"):
        heading = card.find(["h2", "h3", "h4", ".panel-title", ".contest-title"])
        contest = heading.get_text(" ", strip=True) if heading else ""
        if not contest:
            continue
        for tr in card.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 3 and cells[0].lower() not in ("candidate", "choice", "total", "total votes"):
                nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                pcts = [c for c in cells[1:] if "%" in c]
                if nums:
                    rows.append([contest, cells[0], int(nums[-1]), pcts[-1] if pcts else "", NOW])
    return rows

def fetch_sf_web(page):
    page.goto("https://www.sfelections.org/results/20261103w/detail.html", wait_until="networkidle", timeout=30000)
    soup = BeautifulSoup(page.content(), "html.parser")
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    current_contest = ""
    for elem in soup.find_all(["h2", "h3", "tr"]):
        if elem.name in ("h2", "h3"):
            current_contest = elem.get_text(" ", strip=True)
        elif elem.name == "tr" and current_contest:
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in elem.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) >= 3 and cells[0].lower() not in ("candidate", "total", "total votes", "undervotes", "overvotes"):
                nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                pcts = [c for c in cells[1:] if "%" in c]
                if nums:
                    rows.append([current_contest, cells[0], int(nums[-1]), pcts[-1] if pcts else "", NOW])
    return rows

if __name__ == "__main__":
    tabs = {}

    static_sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_LA": fetch_la,
        "RAW_Alameda": fetch_alameda_main,
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
            "RAW_SF": lambda: fetch_sf_web(page),
            "RAW_SanDiego": lambda: fetch_livevoterturnout(page, "sandiegocaenr", "25"),
            "RAW_Riverside": lambda: fetch_livevoterturnout(page, "riversidecaenr", "25"),
            "RAW_SanBernardino": lambda: fetch_livevoterturnout(page, "sanbernardinocaenr", "25"),
            "RAW_Imperial": lambda: fetch_livevoterturnout(page, "imperialcaenr", "25"),
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
