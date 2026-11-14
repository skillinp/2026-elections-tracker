import os
import re
import io
import requests
import pandas as pd
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
    """Removes '{RcvDetailedReport}' XML namespace prefixes."""
    return tag.split("}", 1)[-1] if "}" in tag else tag

def fetch_santa_barbara():
    url = "https://www.sbcvote.com/Elections/results/2026nov03/results-1.htm"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    current_contest = ""

    # Only inspect leaf <tr> elements (skip giant outer layout wrapper rows)
    for tr in soup.find_all("tr"):
        if tr.find("table"):
            continue
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if not cells:
            continue

        # Contest headers look like: "Governor (Vote for 1)" or "Mayor, City of Santa Barbara (Vote for 1)"
        if len(cells) == 1 and "(Vote for" in cells[0]:
            current_contest = cells[0]
            continue

        # Candidate rows have 5 columns: [Candidate, Poll, Mail, Total, Pct]
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

    # Extract race title from Tablix2 Textbox24
    contest_title = "Alameda RCV"
    for elem in root.iter():
        if strip_ns(elem.tag) == "Tablix2" and "Textbox24" in elem.attrib:
            contest_title = elem.attrib["Textbox24"]
            break

    rows = [["Contest", "Round", "Candidate", "Votes", "Status", "Source URL"]]
    current_round = ""

    for elem in root.iter():
        tag = strip_ns(elem.tag)
        if tag == "Tablix5" and "Textbox86" in elem.attrib:
            current_round = elem.attrib["Textbox86"]  # e.g., "Round 1"
        elif tag == "choiceGroup" and current_round:
            # Print one sample choiceGroup structure on first pass to verify exact attributes
            if len(rows) == 1:
                print("Sample choiceGroup children:", [(strip_ns(c.tag), c.attrib) for c in elem.iter()])
            cand_name = ""
            votes = ""
            status = ""
            for child in elem.iter():
                for k, v in child.attrib.items():
                    if any(w in k.lower() for w in ["choice", "candidate", "name", "textbox26", "textbox28"]):
                        if not cand_name and not v.replace(".", "").isdigit():
                            cand_name = v
                    if any(w in k.lower() for w in ["vote", "count", "points"]) and "nontransferable" not in k.lower():
                        if not votes and v.replace(".", "").isdigit():
                            votes = round(float(v))
                    if "status" in k.lower() or "state" in k.lower():
                        status = v
            if cand_name:
                rows.append([contest_title, current_round, cand_name, votes, status, xml_url])

    return rows

def fetch_la():
    url = "https://results.lavote.gov/ElectionResults/GetElectionData?electionID=4348"
    resp = requests.get(url, headers={**HEADERS, "Referer": "https://results.lavote.gov/"}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    inner = payload.get("Data", {})

    if isinstance(inner, dict):
        print("LA Data keys:", list(inner.keys()))
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]

    # Walk inner dict/lists to locate contest objects
    contests = []
    if isinstance(inner, dict):
        for v in inner.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                contests = v
                print("LA sample contest keys:", list(v[0].keys()))
                break

    for c in contests:
        c_title = c.get("ContestTitle") or c.get("Title") or c.get("Name") or ""
        cands = c.get("Candidates") or c.get("CandidateResults") or c.get("Choices") or []
        for cand in cands:
            rows.append([
                c_title,
                cand.get("CandidateName") or cand.get("Name") or cand.get("Title") or "",
                cand.get("TotalVotes") or cand.get("Votes") or 0,
                cand.get("VotePercent") or cand.get("Percent") or "",
                NOW,
            ])
    return rows

def fetch_livevoterturnout(index_url: str):
    resp = requests.get(index_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    csv_link = next(
        a["href"] for a in soup.find_all("a", href=True)
        if "csv" in a.get_text().lower() and "contest" in a.get_text().lower()
    )
    if not csv_link.startswith("http"):
        base = index_url.rsplit("/", 1)[0]
        csv_link = f"{base}/{csv_link.lstrip('./')}"

    csv_text = requests.get(csv_link, headers=HEADERS, timeout=20).text
    df = pd.read_csv(io.StringIO(csv_text))
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for _, r in df.iterrows():
        rows.append([
            str(r.get("Contest Name", r.iloc[0])),
            str(r.get("Candidate Name", r.iloc[1])),
            int(r.get("Total Votes", r.iloc[2])),
            str(r.get("Percentage", "")),
            NOW,
        ])
    return rows

if __name__ == "__main__":
    tabs = {}

    sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_LA": fetch_la,
        "RAW_SanDiego": lambda: fetch_livevoterturnout("https://www.livevoterturnout.com/ENR/sandiegocaenr/25/en/Index_25.html"),
        "RAW_Riverside": lambda: fetch_livevoterturnout("https://www.livevoterturnout.com/ENR/riversidecaenr/12/en/Index_12.html"),
        "RAW_SanBernardino": lambda: fetch_livevoterturnout("https://www.livevoterturnout.com/ENR/sanbernardinocaenr/3/en/Index_3.html"),
        "RAW_Imperial": lambda: fetch_livevoterturnout("https://www.livevoterturnout.com/ENR/imperialcaenr/6/en/Index_6.html"),
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
