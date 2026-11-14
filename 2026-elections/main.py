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

def fetch_la():
    url = "https://results.lavote.gov/ElectionResults/GetElectionData?electionID=4348"
    resp = requests.get(url, headers={**HEADERS, "Referer": "https://results.lavote.gov/"}, timeout=20)
    resp.raise_for_status()
    inner = resp.json().get("Data", {})

    # Build lookup map if candidates are stored in CandidateList keyed by ID or ContestID
    cand_list = inner.get("CandidateList", [])
    if cand_list:
        print("LA sample CandidateList[0]:", cand_list[0])

    groups = inner.get("ContestGroups", [])
    if groups and groups[0].get("Contests"):
        print("LA sample Contest[0]:", groups[0]["Contests"][0])

    # Group candidates by ContestID if CandidateList is flat
    cands_by_contest = {}
    for c in cand_list:
        cid = c.get("ContestID") or c.get("ContestId")
        if cid is not None:
            cands_by_contest.setdefault(cid, []).append(c)

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for grp in groups:
        for contest in grp.get("Contests", []):
            cid = contest.get("ID") or contest.get("Id") or contest.get("ContestID")
            c_title = contest.get("Name") or contest.get("Title") or contest.get("ContestTitle") or ""
            cands = contest.get("Candidates") or cands_by_contest.get(cid, [])
            for cand in cands:
                rows.append([
                    c_title,
                    cand.get("Name") or cand.get("CandidateName") or cand.get("Title") or "",
                    cand.get("Votes") or cand.get("TotalVotes") or 0,
                    cand.get("Percent") or cand.get("VotePercent") or cand.get("Percentage") or "",
                    NOW,
                ])
    return rows

def fetch_alameda_main():
    url = "https://www.alamedacountyca.gov/rovresults/260/"
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
        if len(cells) == 1 and len(cells[0]) > 4 and not cells[0].replace(",", "").isdigit():
            current_contest = cells[0]
        elif current_contest and len(cells) >= 3:
            # Look for candidate name followed by integer vote total and percentage
            nums = [c for c in cells[1:] if c.replace(",", "").isdigit()]
            pcts = [c for c in cells[1:] if "%" in c]
            if nums:
                rows.append([current_contest, cells[0], int(nums[-1].replace(",", "")), pcts[-1] if pcts else "", NOW])
    return rows

def debug_livevoterturnout():
    url = "https://www.livevoterturnout.com/ENR/sandiegocaenr/25/en/Index_25.html"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    links = [a["href"] for a in soup.find_all("a", href=True)]
    scripts = [s["src"] for s in soup.find_all("script", src=True)]
    print("livevoterturnout links:", links[:15])
    print("livevoterturnout scripts:", scripts[:10])

if __name__ == "__main__":
    debug_livevoterturnout()
    tabs = {}

    sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_LA": fetch_la,
        "RAW_Alameda": fetch_alameda_main,
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
