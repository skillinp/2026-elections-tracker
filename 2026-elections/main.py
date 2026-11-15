import os
import re
import io
import requests
import pdfplumber
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

# 3. FIXING VOTES/PCT: Los Angeles
def fetch_la():
    url = "https://results.lavote.gov/ElectionResults/GetElectionData?electionID=4348"
    resp = requests.get(url, headers={**HEADERS, "Referer": "https://results.lavote.gov/"}, timeout=20)
    resp.raise_for_status()
    inner = resp.json().get("Data", {})

    groups = inner.get("ContestGroups") or []
    printed_sample = False

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        contests = grp.get("Contests") or []
        if isinstance(contests, dict):
            contests = list(contests.values())
        for contest in contests:
            if not isinstance(contest, dict):
                continue
            c_title = contest.get("Name") or contest.get("Title") or contest.get("ContestTitle") or ""
            cands = contest.get("Candidates") or []
            for cand in cands:
                if not isinstance(cand, dict):
                    continue
                if not printed_sample:
                    print("LA sample candidate dict:", cand)
                    printed_sample = True
                # Check all common LA JSON keys for votes and percent
                votes = next(
                    (cand[k] for k in ["Votes", "TotalVotes", "VoteCount", "BallotCount", "Count", "YesVotes"] if k in cand and cand[k] is not None),
                    0
                )
                pct = next(
                    (cand[k] for k in ["Percent", "VotePercent", "Percentage", "VotePercentage", "Pct"] if k in cand and cand[k] is not None),
                    ""
                )
                rows.append([
                    c_title,
                    cand.get("Name") or cand.get("CandidateName") or cand.get("Title") or "",
                    votes,
                    pct,
                    NOW,
                ])
    return rows

# 4. NEW: San Francisco (Static HTML summary tables)
def fetch_sf():
    url = "https://www.sfelections.org/results/20261103w/index.html"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for table in soup.find_all("table"):
        # SF tables typically put the contest title in a preceding heading or <caption>/<th>
        caption = table.find("caption")
        prev_h = table.find_previous(["h2", "h3", "h4"])
        contest = (caption.get_text(" ", strip=True) if caption else (prev_h.get_text(" ", strip=True) if prev_h else ""))
        for tr in table.find_all("tr"):
            cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
            if len(cells) >= 3 and any(c.replace(",", "").isdigit() for c in cells[1:]):
                cand = cells[0]
                if cand.lower() in ("candidate", "total", "total votes", "undervotes", "overvotes"):
                    continue
                nums = [c.replace(",", "") for c in cells[1:] if c.replace(",", "").isdigit()]
                pcts = [c for c in cells[1:] if "%" in c]
                if nums and contest:
                    rows.append([contest, cand, int(nums[0]), pcts[0] if pcts else "", NOW])
    return rows

# 5. NEW: Sacramento (Auto-finds newest PDF on the official schedule page and parses it)
def fetch_sacramento():
    sched_url = "https://elections.saccounty.gov/us/en/election-information/november-3--2026--general-election-results-schedule.html"
    resp = requests.get(sched_url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_links = [
        a["href"] for a in soup.find_all("a", href=True)
        if a["href"].lower().endswith(".pdf") and "november-2026" in a["href"].lower()
    ]
    if not pdf_links:
        return [["Contest", "Candidate", "Votes", "Pct", "Updated"]]

    # Pick the newest dated PDF (e.g. 11132026.pdf > 11102026.pdf)
    latest_pdf = sorted(set(pdf_links))[-1]
    if not latest_pdf.startswith("http"):
        latest_pdf = f"https://elections.saccounty.gov{latest_pdf}"
    print("Sacramento latest PDF:", latest_pdf)

    pdf_bytes = requests.get(latest_pdf, headers=HEADERS, timeout=30).content
    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    current_contest = ""

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = line.strip()
                if "(Vote for" in line or "MEASURE " in line.upper():
                    current_contest = line
                    continue
                # Match lines ending with vote count and percentage: e.g. "JANE DOE 123,456 54.32%"
                m = re.match(r"^(.+?)\s+([\d,]+)\s+([\d.]+%)$", line)
                if m and current_contest:
                    cand, votes_str, pct = m.groups()
                    if cand.lower() not in ("total", "times cast", "undervotes", "overvotes"):
                        rows.append([current_contest, cand.strip(), int(votes_str.replace(",", "")), pct, NOW])
    return rows

# 6. QUICK INSPECT: Alameda Main HTML + livevoterturnout JS endpoint
def inspect_remaining():
    ala = requests.get("https://www.alamedacountyca.gov/rovresults/260/", headers=HEADERS, timeout=20).text
    # Find where a major race like "Governor" appears in Alameda's HTML so we see its exact tags
    idx = ala.find("Governor")
    if idx != -1:
        print("Alameda HTML around Governor:", re.sub(r"\s+", " ", ala[max(0, idx-200):idx+400]))

    js_text = requests.get("https://www.livevoterturnout.com/shared/scripts/LiveResultsScripts_v4.2.js", headers=HEADERS, timeout=20).text
    ajax_calls = re.findall(r'(?:url|getJSON|ajax)\s*[:(]\s*([^,\n\r)]{5,120})', js_text)
    print("livevoterturnout AJAX calls:", ajax_calls[:15])

if __name__ == "__main__":
    inspect_remaining()
    tabs = {}

    sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_LA": fetch_la,
        "RAW_SF": fetch_sf,
        "RAW_Sacramento": fetch_sacramento,
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
