import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime

WEBHOOK_URL = os.environ["APPS_SCRIPT_URL"]
SECRET_TOKEN = os.environ["WEBHOOK_SECRET"]
NOW = datetime.now().strftime("%Y-%m-%d %H:%M")

def fetch_clarity_county(base_url: str):
    """Automatically grabs the latest version number from current_ver.txt, then fetches sum.json."""
    ver = requests.get(f"{base_url}/current_ver.txt", timeout=15).text.strip()
    data = requests.get(f"{base_url}/{ver}/json/sum.json", timeout=20).json()
    
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
    data = requests.get(url, timeout=20).json()
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    # Loop through LA's JSON structure (adjust keys after inspecting one sample response)
    for contest in data.get("Contests", []):
        for cand in contest.get("Candidates", []):
            rows.append([contest.get("Title"), cand.get("Name"), cand.get("Votes"), cand.get("Percent"), NOW])
    return rows

def fetch_alameda_rcv(xml_url: str):
    resp = requests.get(xml_url, timeout=20)
    root = ET.fromstring(resp.content)
    rows = [["Contest", "Round", "Candidate", "Votes", "Status", "Source URL"]]
    # Parses the RcvDetailedReport.xml tree into tidy rows
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
    payload = {
        "secret": SECRET_TOKEN,
        "tabs": {
            "RAW_ContraCosta": fetch_clarity_county("https://results.enr.clarityelections.com/CA/Contra_Costa/127405"),
            "RAW_Butte": fetch_clarity_county("https://results.enr.clarityelections.com/CA/Butte/126778"),
            "RAW_LA": fetch_la(),
            "RCV_Alameda": fetch_alameda_rcv(
                "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
            ),
        }
    }
    res = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    print("Sheet update status:", res.text)
