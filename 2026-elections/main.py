import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

WEBHOOK_URL = os.environ["APPS_SCRIPT_URL"]
SECRET_TOKEN = os.environ["WEBHOOK_SECRET"]
NOW = datetime.now().strftime("%Y-%m-%d %H:%M")

# Pretend to be a standard Chrome browser so county firewalls (CloudFront/Akamai) don't block Python
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://results.enr.clarityelections.com/"
}

def fetch_clarity_county(base_url: str):
    """Automatically grabs the latest version number from current_ver.txt, then fetches sum.json."""
    ver_resp = requests.get(f"{base_url}/current_ver.txt", headers=HEADERS, timeout=15)
    ver_resp.raise_for_status()
    ver = ver_resp.text.strip()

    data_resp = requests.get(f"{base_url}/{ver}/json/sum.json", headers=HEADERS, timeout=20)
    data_resp.raise_for_status()
    data = data_resp.json()

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for contest in data.get("Contests", []):
        c_name = contest.get("C", "")
        candidates = contest.get("CH", [])
        votes = contest.get("V", [])
        pcts = contest.get("PCT", [])
        for cand, v, p in zip(candidates, votes, pcts):
            rows.append([c_name, cand, v, p, NOW])
    return rows

def fetch_la():
    url = "https://results.lavote.gov/ElectionResults/GetElectionData?electionID=4348"
    la_headers = {**HEADERS, "Referer": "https://results.lavote.gov/"}
    resp = requests.get(url, headers=la_headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for contest in data.get("Contests", []):
        for cand in contest.get("Candidates", []):
            rows.append([contest.get("Title"), cand.get("Name"), cand.get("Votes"), cand.get("Percent"), NOW])
    return rows

def fetch_alameda_rcv(xml_url: str):
    resp = requests.get(xml_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    rows = [["Contest", "Round", "Candidate", "Votes", "Status", "Source URL"]]
    contest_title = root.attrib.get("title", "Alameda RCV Race")
    for rnd in root.findall(".//Round"):
        r_num = rnd.attrib.get("number", "")
        for cand in rnd.findall(".//Candidate"):
            rows.append([
                contest_title,
                r_num,
                cand.attrib.get("name", ""),
                cand.attrib.get("votes", "0"),
                cand.attrib.get("status", ""),
                xml_url
            ])
    return rows

if __name__ == "__main__":
    tabs = {}

    sources = {
        "RAW_ContraCosta": lambda: fetch_clarity_county("https://results.enr.clarityelections.com/CA/Contra_Costa/127405"),
        "RAW_Butte": lambda: fetch_clarity_county("https://results.enr.clarityelections.com/CA/Butte/126778"),
        "RAW_LA": fetch_la,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
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
