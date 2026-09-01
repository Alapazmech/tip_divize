"""Stahování zápasů z ceskyflorbal.cz.

Web je server-rendered (Nette) a celý los soutěže vrací v jednom HTML na adrese
/competition/detail/matches/<kód>?divisionAlias=<alias>&competitionFisId=<ročník>.
Žádné API není potřeba — parsují se bloky <div class="Match">.

U odehraných zápasů s rozdílem jednoho gólu se navíc stahuje detail zápasu
(/match/detail/default/<id>): tabulka třetin má sloupce „prodloužení" a
„nájezdy" jen tehdy, když k nim došlo — z toho se pozná výsledek základní
hrací doby (nutné pro vypořádání trhů 1/10/02/2). Detaily se cachují
v data/details/, každý se stahuje jen jednou.

Použití:
    python3 scraper.py season    # aktuální sezóna (+ detaily) -> data/season.json
    python3 scraper.py history   # loňské soutěže (seed modelu) -> data/history/*.json
"""

import html
import json
import pathlib
import re
import sys
import time
import urllib.request

BASE = "https://www.ceskyflorbal.cz/competition/detail/matches"
DETAIL = "https://www.ceskyflorbal.cz/match/detail/default"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) tipdivize/1.0"

DATA = pathlib.Path(__file__).parent / "data"
DETAILS = DATA / "details"

# Aktuální sezóna: Divize mužů, skupina B, ročník 2026/2027
SEASON = {
    "code": "8XM4",
    "alias": "8XM4-B",
    "fis_id": 4822,
    "label": "Divize B 2026/27",
    "start_year": 2026,
}

# Historie pro nasazení kurzového modelu (ročník 2025/2026).
# `prior` je startovní Elo — Národní liga je o soutěž výš než Divize.
HISTORY = [
    {
        "code": "8XM4",
        "alias": "8XM4-B",
        "fis_id": 4490,
        "label": "Divize B 2025/26",
        "start_year": 2025,
        "prior": 1500,
    },
    {
        "code": "8XM4",
        "alias": "8XM4-A",
        "fis_id": 4490,
        "label": "Divize A 2025/26",
        "start_year": 2025,
        "prior": 1500,
    },
    {
        "code": "8XM4",
        "alias": "8XM4-C",
        "fis_id": 4490,
        "label": "Divize C 2025/26",
        "start_year": 2025,
        "prior": 1500,
    },
    {
        "code": "8XM4",
        "alias": "8XM4-D",
        "fis_id": 4490,
        "label": "Divize D 2025/26",
        "start_year": 2025,
        "prior": 1500,
    },
    {
        "code": "8XM4",
        "alias": "8XM4-E",
        "fis_id": 4490,
        "label": "Divize E 2025/26",
        "start_year": 2025,
        "prior": 1500,
    },
    {
        "code": "8XM3",
        "alias": "8XM3-A",
        "fis_id": 4490,
        "label": "Národní liga západ 2025/26",
        "start_year": 2025,
        "prior": 1620,
    },
    {
        "code": "8XM3",
        "alias": "8XM3-B",
        "fis_id": 4490,
        "label": "Národní liga východ 2025/26",
        "start_year": 2025,
        "prior": 1620,
    },
]

MONTHS_FIRST_HALF = {8, 9, 10, 11, 12}  # srpen–prosinec = první rok sezóny


def fetch(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def parse_date(raw: str, start_year: int) -> str | None:
    """'SO, 12. 9.' -> '2026-09-12' (rok podle poloviny sezóny)."""
    m = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.", raw)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = start_year if month in MONTHS_FIRST_HALF else start_year + 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_matches(page: str, start_year: int) -> list[dict]:
    matches = []
    for block in page.split('<div class="Match">')[1:]:
        names = re.findall(r'Match-teamName">([^<]+)</p>', block)
        shorts = re.findall(r'Match-teamNameShort">([^<]+)</p>', block)
        if len(names) != 2:
            continue
        round_m = re.search(r'Match-round">([^<]+)<', block)
        date_m = re.search(r'Match-date"[^>]*>([^<]+)<', block)
        score_m = re.search(
            r'Match-score">\s*<a href="/match/detail/default/(\d+)"[^>]*>\s*(\d+):(\d+)([^<]*)<',
            block,
        )
        time_m = re.search(
            r'Match-startTime">\s*<a href="/match/detail/default/(\d+)">\s*(\d{1,2}:\d{2})',
            block,
        )
        place_m = re.search(r'Match-place">([^<]*)<', block)
        status_m = re.search(r'Match-status">([^<]*)<', block)

        round_label = html.unescape(round_m.group(1).strip()) if round_m else ""
        round_num_m = re.match(r"(\d+)\. kolo", round_label)
        match: dict = {
            "id": int((score_m or time_m).group(1)) if (score_m or time_m) else None,
            "round": int(round_num_m.group(1)) if round_num_m else None,
            "round_label": round_label,
            "date": parse_date(date_m.group(1), start_year) if date_m else None,
            "time": time_m.group(2) if time_m else None,
            "home": html.unescape(names[0].strip()),
            "away": html.unescape(names[1].strip()),
            "home_short": (
                html.unescape(shorts[0].strip()) if len(shorts) == 2 else None
            ),
            "away_short": (
                html.unescape(shorts[1].strip()) if len(shorts) == 2 else None
            ),
            "place": html.unescape(place_m.group(1).strip()) if place_m else "",
            "status": html.unescape(status_m.group(1).strip()) if status_m else "",
            "score": None,
        }
        if score_m:
            extra = score_m.group(4).strip()  # případné 'p'/'sn' za skóre
            match["score"] = [int(score_m.group(2)), int(score_m.group(3))]
            match["score_note"] = extra
        matches.append(match)
    return matches


def parse_detail(page: str) -> dict:
    """Z tabulky třetin (MatchCenter) zjistí prodloužení/nájezdy."""
    i = page.find("MatchCenter-table")
    if i < 0:
        return {"overtime": None, "shootout": None}
    thead_m = re.search(r"<thead>(.*?)</thead>", page[i:], re.S)
    thead = thead_m.group(1) if thead_m else ""
    return {"overtime": "prodloužení" in thead, "shootout": "nájezdy" in thead}


def match_detail(match_id: int) -> dict:
    """Detail zápasu s cache — každý se stahuje jen jednou."""
    DETAILS.mkdir(parents=True, exist_ok=True)
    cache = DETAILS / f"{match_id}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    detail = parse_detail(fetch(f"{DETAIL}/{match_id}"))
    detail["id"] = match_id
    cache.write_text(json.dumps(detail, ensure_ascii=False))
    time.sleep(0.5)
    return detail


def enrich_with_details(matches: list[dict]) -> int:
    """Doplní overtime/shootout k odehraným zápasům.

    Do prodloužení mohl jít jen zápas s rozdílem jednoho gólu (prodloužení
    končí prvním gólem, nájezdy se počítají jako jeden gól) — ostatní se
    neřeší a detail se nestahuje.
    """
    fetched = 0
    for m in matches:
        if not m["score"]:
            continue
        gh, ga = m["score"]
        if abs(gh - ga) != 1:
            m["overtime"] = False
            m["shootout"] = False
            continue
        cached = (DETAILS / f"{m['id']}.json").exists()
        detail = match_detail(m["id"])
        fetched += 0 if cached else 1
        m["overtime"] = detail["overtime"]
        m["shootout"] = detail["shootout"]
    return fetched


def url_for(cfg: dict) -> str:
    return f"{BASE}/{cfg['code']}?divisionAlias={cfg['alias']}&competitionFisId={cfg['fis_id']}"


def scrape(cfg: dict) -> dict:
    page = fetch(url_for(cfg))
    matches = parse_matches(page, cfg["start_year"])
    if not matches:
        raise RuntimeError(
            f"{cfg['label']}: na stránce nejsou žádné zápasy — změnila se struktura webu?"
        )
    return {
        "label": cfg["label"],
        "url": url_for(cfg),
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prior": cfg.get("prior", 1500),
        "matches": matches,
    }


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "season"
    DATA.mkdir(exist_ok=True)
    if mode == "season":
        data = scrape(SEASON)
        fetched = enrich_with_details(data["matches"])
        played = sum(1 for m in data["matches"] if m["score"])
        (DATA / "season.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1)
        )
        print(
            f"{data['label']}: {len(data['matches'])} zápasů, {played} odehráno,"
            f" {fetched} nových detailů -> data/season.json"
        )
    elif mode == "history":
        (DATA / "history").mkdir(exist_ok=True)
        for cfg in HISTORY:
            data = scrape(cfg)
            out = DATA / "history" / f"{cfg['alias']}_{cfg['fis_id']}.json"
            out.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            print(
                f"{data['label']}: {len(data['matches'])} zápasů -> {out.relative_to(DATA.parent)}"
            )
            time.sleep(1)
    else:
        sys.exit(f"Neznámý režim: {mode} (použij 'season' nebo 'history')")


if __name__ == "__main__":
    main()
