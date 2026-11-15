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

# 3. WORKING: Alameda Main
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

# 5. FIXED: Los Angeles (maps ReferenceID + Value from GetCounterData)
def fetch_la():
    base = "https://results.lavote.gov/ElectionResults"
    hdrs = {**HEADERS, "Referer": "https://results.lavote.gov/"}

    meta = requests.get(f"{base}/GetElectionData?electionID=4348", headers=hdrs, timeout=20).json().get("Data", {})
    counter_raw = requests.get(f"{base}/GetCounterData?electionID=4348", headers=hdrs, timeout=20).json()
    counters = counter_raw.get("Data", counter_raw)

    # Build lookup: ReferenceID -> Value for all candidate counters
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

# 6. NEW: Butte County via Clarity JSON (no scrolling needed)
def fetch_clarity_json(county_slug: str, election_id: str, version_id: str):
    base = f"https://results.enr.clarityelections.com/CA/{county_slug}/{election_id}/{version_id}/json"
    settings = requests.get(f"{base}/en/electionsettings.json", headers=HEADERS, timeout=20).json()
    summary = requests.get(f"{base}/sum.json", headers=HEADERS, timeout=20).json()

    # Map contest ID -> {title, candidates: [names]}
    contest_meta = {}
    for c in settings.get("Contests", []):
        cid = str(c.get("K") or c.get("Id") or c.get("id") or "")
        title = c.get("C") or c.get("Name") or c.get("Title") or ""
        cands = [ch.get("N") or ch.get("Name") or "" for ch in (c.get("CH") or c.get("Candidates") or [])]
        if cid:
            contest_meta[cid] = {"title": title, "cands": cands}

    rows = [["Contest", "Candidate", "Votes", "Pct", "Updated"]]
    for item in summary.get("Contests", []):
        cid = str(item.get("K") or item.get("Id") or item.get("id") or "")
        meta = contest_meta.get(cid, {})
        title = meta.get("title") or item.get("C") or f"Contest {cid}"
        cands = meta.get("cands") or item.get("CH") or []
        votes_list = item.get("V") or item.get("Votes") or []
        pct_list = item.get("P") or item.get("Pct") or []

        for idx, v in enumerate(votes_list):
            cand_name = cands[idx] if idx < len(cands) else f"Option {idx + 1}"
            votes = int(str(v).replace(",", "")) if str(v).replace(",", "").isdigit() else 0
            pct = f"{pct_list[idx]}%" if idx < len(pct_list) and "%" not in str(pct_list[idx]) else (str(pct_list[idx]) if idx < len(pct_list) else "")
            rows.append([title, cand_name, votes, pct, NOW])

    if len(rows) == 1:
        print(f"Clarity [{county_slug}] sample keys:", list(summary.keys()), str(summary)[:300])
    return rows

# 7. INSPECTING SF & livevoterturnout XHRs
def inspect_sf_and_lvt(page):
    # Check SF via requests (since requests bypassed CloudFront 403 earlier)
    sf_resp = requests.get("https://www.sfelections.org/results/20261103w/index.html", headers=HEADERS, timeout=20)
    sf_soup = BeautifulSoup(sf_resp.text, "html.parser")
    scripts = [s.get("src") for s in sf_soup.find_all("script") if s.get("src")]
    links = [a.get("href") for a in sf_soup.find_all("a", href=True) if "detail" in a.get("href", "") or "summary" in a.get("href", "") or "xml" in a.get("href", "") or "json" in a.get("href", "")]
    print("SF scripts/links:", scripts[:8], links[:8])

    # Check San Diego livevoterturnout XHRs on working Index_25.html
    sd_xhrs = []
    page.on("response", lambda r: sd_xhrs.append(r.url) if "livevoterturnout" in r.url and not r.url.endswith((".js", ".css", ".png", ".woff2")) else None)
    page.goto("https://www.livevoterturnout.com/ENR/sandiegocaenr/25/en/Index_25.html", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)
    print("SD livevoterturnout data URLs:", sd_xhrs)

if __name__ == "__main__":
    tabs = {}

    static_sources = {
        "RAW_SantaBarbara": fetch_santa_barbara,
        "RCV_Alameda": lambda: fetch_alameda_rcv(
            "https://www.alamedacountyca.gov/rovresults/rcv/260/Oakland/008-SchoolDirector_District_6/RcvDetailedReport.xml"
        ),
        "RAW_Alameda": fetch_alameda_main,
        "RAW_LA": fetch_la,
        "RAW_Butte": lambda: fetch_clarity_json("Butte", "126778", "387043"),
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

        try:
            rows = fetch_sacramento_web(page)
            tabs["RAW_Sacramento"] = rows
            print(f"[OK] RAW_Sacramento: fetched {len(rows) - 1} rows")
        except Exception as e:
            print(f"[ERROR] RAW_Sacramento failed: {e}")

        try:
            inspect_sf_and_lvt(page)
        except Exception as e:
            print(f"[ERROR] inspect failed: {e}")

        browser.close()

    if tabs:
        res = requests.post(
            WEBHOOK_URL,
            json={"secret": SECRET_TOKEN, "tabs": tabs},
            timeout=30
        )
        print("Google Sheet response:", res.status_code, res.text)
