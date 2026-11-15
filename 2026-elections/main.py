import os
import re
import io
import csv
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime

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

# 3. FIXING: Los Angeles (safe inspection + extraction)
def fetch_la():
    url = "https://results.lavote.gov/ElectionResults/GetElectionData?electionID=4348"
    resp = requests.get(url, headers={**HEADERS, "Referer": "https://results.lavote.gov/"}, timeout=20)
    resp.raise_for_status()
    inner = resp.json().get("Data", {})

    cand_list = inner.get("CandidateList") or []
    groups = inner.get("ContestGroups") or []

    if isinstance(cand_list, list) and cand_list:
        print("LA CandidateList[0]:", cand_list[0])
    elif isinstance(cand_list, dict) and cand_list:
        first_k = next(iter(cand_list))
        print("LA CandidateList dict sample:", first_k, cand_list[first_k])

    if isinstance(groups, list) and groups:
        print("LA ContestGroups[0]:", str(groups[0])[:300])

    # Build candidate lookup by ContestID if CandidateList is a list of dicts
    cands_by_contest = {}
    if isinstance(cand_list, list):
        for c in cand_list:
            if isinstance(c, dict):
                cid = c.get("ContestID") or c.get("ContestId") or c.get("CID")
                if cid is not None:
                    cands_by_contest.setdefault(cid, []).append(c)

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    if isinstance(groups, list):
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            contests = grp.get("Contests") or []
            if isinstance(contests, dict):
                contests = list(contests.values())
            for contest in contests:
                if not isinstance(contest, dict):
                    continue
                cid = contest.get("ID") or contest.get("Id") or contest.get("ContestID")
                c_title = contest.get("Name") or contest.get("Title") or contest.get("ContestTitle") or ""
                cands = contest.get("Candidates") or cands_by_contest.get(cid, [])
                for cand in cands:
                    if not isinstance(cand, dict):
                        continue
                    rows.append([
                        c_title,
                        cand.get("Name") or cand.get("CandidateName") or cand.get("Title") or "",
                        cand.get("Votes") or cand.get("TotalVotes") or 0,
                        cand.get("Percent") or cand.get("VotePercent") or cand.get("Percentage") or "",
                        NOW,
                    ])
    return rows

# 4. DISCOVERY: Inspect Alameda Main, livevoterturnout JS endpoints, SF, and OC
def discover_remaining_counties():
    # A. Alameda Main HTML structure
    ala = requests.get("https://www.alamedacountyca.gov/rovresults/260/", headers=HEADERS, timeout=20)
    soup_ala = BeautifulSoup(ala.text, "html.parser")
    print("Alameda main links:", [a["href"] for a in soup_ala.find_all("a", href=True)][:15])
    print("Alameda main text sample:", re.sub(r"\s+", " ", soup_ala.get_text())[:300])

    # B. Find exact JSON endpoint inside livevoterturnout's LiveResultsScripts_v4.2.js
    js_url = "https://www.livevoterturnout.com/shared/scripts/LiveResultsScripts_v4.2.js"
    js_text = requests.get(js_url, headers=HEADERS, timeout=20).text
    json_paths = set(re.findall(r'["\']([^"\']*\.(?:json|csv|xml)[^"\']*)["\']', js_text, re.I))
    print("livevoterturnout JS data paths:", list(json_paths)[:20])

    # C. San Francisco links (XML/CSV/Summary)
    sf = requests.get("https://www.sfelections.org/results/20261103w/index.html", headers=HEADERS, timeout=20)
    soup_sf = BeautifulSoup(sf.text, "html.parser")
    print("SF links:", [a["href"] for a in soup_sf.find_all("a", href=True)][:20])

    # D. Orange County links/scripts
    oc = requests.get("https://ocvote.gov/results/current-election-results", headers=HEADERS, timeout=20)
    soup_oc = BeautifulSoup(oc.text, "html.parser")
    oc_data_links = [
        a["href"] for a in soup_oc.find_all("a", href=True)
        if any(ext in a["href"].lower() for ext in [".csv", ".xml", ".json", ".txt", ".pdf", "result"])
    ]
    print("OC data links:", oc_data_links[:15])

if __name__ == "__main__":
    discover_remaining_counties()
    tabs = {}

    sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_LA": fetch_la,
    }

    for tab_name, fetcher in sources.items():
        try:
            rows = fetcher()
            tabs[tab_name] = rows
            print(f"[OK] {tab_name}: fetched {len(rows) - 1} rows")
        except Exception as e:
            print(f"[ERROR] {tab_name} failed: {e}")

    if tabs:
        res = requests.post(
            WEBHOOK_URL,
            json={"secret": SECRET_TOKEN, "tabs": tabs},
            timeout=30
        )
        print("Google Sheet response:", res.status_code, res.text)
