import os
import re
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

def debug_clarity(base_url: str):
    ver_resp = requests.get(f"{base_url}/current_ver.txt", headers=HEADERS, timeout=15)
    print(f"Clarity {base_url} current_ver status={ver_resp.status_code}, text={ver_resp.text[:100]!r}")
    ver = ver_resp.text.strip()
    if ver.isdigit():
        sum_resp = requests.get(f"{base_url}/{ver}/json/sum.json", headers=HEADERS, timeout=20)
        print(f"Clarity sum.json status={sum_resp.status_code}, len={len(sum_resp.text)}, head={sum_resp.text[:120]!r}")
    return []

def fetch_la():
    url = "https://results.lavote.gov/ElectionResults/GetElectionData?electionID=4348"
    resp = requests.get(url, headers={**HEADERS, "Referer": "https://results.lavote.gov/"}, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # Log top-level structure so we can see exact keys in GitHub Actions logs
    if isinstance(data, dict):
        print("LA JSON top keys:", list(data.keys()))
    elif isinstance(data, list) and data:
        print("LA JSON list[0] keys:", list(data[0].keys()) if isinstance(data[0], dict) else type(data[0]))

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    # LA's GetElectionData typically nests contests under Election -> ContestList or Contests
    contests = (
        data.get("Contests")
        or data.get("ContestList")
        or (data.get("Election", {}) if isinstance(data.get("Election"), dict) else {}).get("Contests")
        or []
    )
    for contest in contests:
        c_title = contest.get("Title") or contest.get("ContestTitle") or contest.get("Name") or ""
        cands = contest.get("Candidates") or contest.get("CandidateList") or []
        for cand in cands:
            rows.append([
                c_title,
                cand.get("Name") or cand.get("CandidateName") or "",
                cand.get("Votes") or cand.get("TotalVotes") or 0,
                cand.get("Percent") or cand.get("VotePercent") or "",
                NOW,
            ])
    return rows

def fetch_alameda_rcv(xml_url: str):
    resp = requests.get(xml_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    print("Alameda RCV root tag:", root.tag, "attribs:", root.attrib)
    print("Alameda RCV child tags:", [child.tag for child in list(root)[:5]])

    rows = [["Contest", "Round", "Candidate", "Votes", "Status", "Source URL"]]
    # Print first few nested elements if Round isn't found directly
    rounds = root.findall(".//Round") or root.findall(".//round")
    if not rounds:
        for elem in list(root.iter())[:15]:
            print("  XML elem:", elem.tag, elem.attrib, (elem.text or "").strip()[:40])
    for rnd in rounds:
        r_num = rnd.attrib.get("number") or rnd.attrib.get("Number") or ""
        for cand in rnd:
            rows.append([
                root.attrib.get("title", "Alameda RCV"),
                r_num,
                cand.attrib.get("name") or cand.attrib.get("Name") or cand.text or "",
                cand.attrib.get("votes") or cand.attrib.get("Votes") or "0",
                cand.attrib.get("status") or cand.attrib.get("Status") or "",
                xml_url,
            ])
    return rows

def fetch_santa_barbara():
    url = "https://www.sbcvote.com/Elections/results/2026nov03/results-1.htm"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    current_contest = "Unknown Contest"
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) == 1 and len(cells[0]) > 3:
            current_contest = cells[0]
        elif len(cells) >= 3 and any(c.replace(",", "").isdigit() for c in cells[1:]):
            cand = cells[0]
            nums = [c for c in cells[1:] if c]
            votes = nums[0] if len(nums) > 0 else ""
            pct = nums[1] if len(nums) > 1 else ""
            rows.append([current_contest, cand, votes, pct, NOW])
    return rows

if __name__ == "__main__":
    tabs = {}

    # Debug Clarity response
    debug_clarity("https://results.enr.clarityelections.com/CA/Contra_Costa/127405")

    sources = {
        "RAW_LA": fetch_la,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_SantaBarbara": fetch_santa_barbara,
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
