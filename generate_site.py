"""Generuje statický index.html: záložky „Divize Sázky" a „Los a tabulka".

Vstupy: data/season.json (los + výsledky), data/published.json (zmrazené
kurzy), data/bets.csv (tikety). Vypořádání: trhy 1/10/02/2 se vztahují
k ZÁKLADNÍ HRACÍ DOBĚ (prodloužení/nájezdy = remíza v základní době).
Na zápasy Bohemians jsou vypsané jen výhry (1 a 2) — žádné zajišťování.

Sázky v bets.csv: round,person,ticket,match,market,stake — `match` je id
zápasu, nebo jednoznačný kus jména týmu v daném kole (např. „Olymp").
Řádky se stejným (round, person, ticket) tvoří jeden AKO tiket: kurzy legů
se násobí a vyjít musí všechny; vklad platí ten z prvního řádku tiketu.
Prázdný `ticket` = sólo tiket.
"""

import collections
import csv
import datetime
import html
import json
import pathlib
import unicodedata

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"

OUR_TEAM = "FbŠ Florbal Bohemians"
START_BANK = 1000
MARKETS = ("1", "10", "02", "2")
MARKET_LABEL = {
    "1": "výhra domácích",
    "10": "neprohra domácích",
    "02": "neprohra hostů",
    "2": "výhra hostů",
}
WINS = {"1": {"1"}, "10": {"1", "0"}, "02": {"0", "2"}, "2": {"2"}}

DAYS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]


def e(s) -> str:
    return html.escape(str(s) if s is not None else "")


def cz_date(iso: str | None) -> str:
    if not iso:
        return "?"
    d = datetime.date.fromisoformat(iso)
    return f"{DAYS[d.weekday()]} {d.day}. {d.month}."


def reg_outcome(m: dict) -> str | None:
    """Výsledek základní hrací doby: '1' / '0' / '2' (None = neodehráno)."""
    if not m["score"]:
        return None
    if m.get("overtime") or m.get("shootout"):
        return "0"
    gh, ga = m["score"]
    return "1" if gh > ga else "2"


def load_csv(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [
            row
            for row in csv.DictReader(f)
            if any((v or "").strip() for v in row.values())
        ]


def fold(s: str) -> str:
    """Malá písmena bez diakritiky — ať „rudna“ najde Rudnou."""
    return "".join(
        c
        for c in unicodedata.normalize("NFD", s.lower())
        if not unicodedata.combining(c)
    )


def resolve_match(ref: str, round_matches: list[dict]) -> dict | None:
    ref = ref.strip()
    if ref.isdigit():
        hits = [m for m in round_matches if m["id"] == int(ref)]
    else:
        low = fold(ref)
        hits = [
            m
            for m in round_matches
            if low in fold(m["home"])
            or low in fold(m["away"])
            or low in fold(m["home_short"] or "")
            or low in fold(m["away_short"] or "")
        ]
    return hits[0] if len(hits) == 1 else None


def settle(matches: list[dict], published: dict) -> dict:
    """Projde kola chronologicky, vypořádá tikety -> stav bank."""
    by_round: dict[int, list[dict]] = collections.defaultdict(list)
    for m in matches:
        if m["round"]:
            by_round[m["round"]].append(m)

    bets = load_csv(DATA / "bets.csv")
    banks: dict[str, float] = {}
    settled_rows: dict[int, list[dict]] = collections.defaultdict(list)
    open_rows: dict[int, list[dict]] = collections.defaultdict(list)
    warnings: list[str] = []

    # řádky -> tikety: stejné (round, person, ticket) = jeden AKO tiket
    grouped: dict[tuple, list[dict]] = collections.defaultdict(list)
    auto = 0
    for b in bets:
        label = (b.get("ticket") or "").strip()
        if not label:
            auto += 1
            label = f"_solo{auto}"
        grouped[(int(b["round"]), b["person"].strip(), label)].append(b)

    tickets = []
    for (rnd, person, label), rows in grouped.items():
        legs = []
        ok = True
        for b in rows:
            match = resolve_match(b["match"], by_round.get(rnd, []))
            if not match:
                warnings.append(
                    f"bets.csv: nejednoznačný zápas '{b['match']}' v {rnd}. kole — tiket ignorován"
                )
                ok = False
                break
            entry = published.get(str(match["id"]))
            if not entry:
                warnings.append(
                    f"bets.csv: zápas {match['home']} - {match['away']} nemá vypsaný kurz"
                )
                ok = False
                break
            if b["market"] not in entry["odds"]:
                what = (
                    "na zápas Bohemians jde vsadit jedině výhra Bohemky"
                    if entry.get("special") and b["market"] in MARKETS
                    else f"neznámý trh '{b['market']}'"
                )
                warnings.append(
                    f"bets.csv: {what} ({person}, {rnd}. kolo) — tiket ignorován"
                )
                ok = False
                break
            legs.append(
                {
                    "match": match,
                    "market": b["market"],
                    "odd": entry["odds"][b["market"]],
                }
            )
        if not ok or not legs:
            continue
        total_odd = 1.0
        for leg in legs:
            total_odd *= leg["odd"]
        tickets.append(
            {
                "round": rnd,
                "person": person,
                "legs": legs,
                "stake": float(rows[0]["stake"]),
                "odd": round(total_odd, 2),
            }
        )
        banks.setdefault(person, START_BANK)

    for rnd in sorted(by_round):
        stakes_this_round: dict[str, float] = collections.defaultdict(float)
        for t in [x for x in tickets if x["round"] == rnd]:
            outcomes = [reg_outcome(leg["match"]) for leg in t["legs"]]
            if any(o is None for o in outcomes):
                open_rows[rnd].append(t)
                stakes_this_round[t["person"]] += t["stake"]
                continue
            leg_wins = [o in WINS[leg["market"]] for o, leg in zip(outcomes, t["legs"])]
            won = all(leg_wins)
            delta = t["stake"] * (t["odd"] - 1) if won else -t["stake"]
            banks[t["person"]] += delta
            settled_rows[rnd].append(
                {**t, "won": won, "delta": delta, "leg_wins": leg_wins}
            )
        for person, staked in stakes_this_round.items():
            if staked > banks[person]:
                warnings.append(
                    f"{person} má v {rnd}. kole vsazeno {staked:.0f}, ale bank je {banks[person]:.0f}"
                )

    return {
        "banks": banks,
        "settled": settled_rows,
        "open": open_rows,
        "warnings": warnings,
    }


def standings(matches: list[dict]) -> list[dict]:
    table: dict[str, dict] = {}
    for m in matches:
        for t in (m["home"], m["away"]):
            table.setdefault(
                t,
                {
                    "team": t,
                    "z": 0,
                    "v": 0,
                    "vp": 0,
                    "pp": 0,
                    "p": 0,
                    "gf": 0,
                    "ga": 0,
                    "b": 0,
                },
            )
    for m in matches:
        if not m["score"]:
            continue
        gh, ga = m["score"]
        ot = bool(m.get("overtime") or m.get("shootout"))
        h, a = table[m["home"]], table[m["away"]]
        h["z"] += 1
        a["z"] += 1
        h["gf"] += gh
        h["ga"] += ga
        a["gf"] += ga
        a["ga"] += gh
        win, lose = (h, a) if gh > ga else (a, h)
        if ot:
            win["vp"] += 1
            win["b"] += 2
            lose["pp"] += 1
            lose["b"] += 1
        else:
            win["v"] += 1
            win["b"] += 3
            lose["p"] += 1
    return sorted(
        table.values(), key=lambda r: (-r["b"], -(r["gf"] - r["ga"]), -r["gf"])
    )


def score_html(m: dict) -> str:
    note = (
        " p"
        if m.get("overtime") and not m.get("shootout")
        else (" sn" if m.get("shootout") else "")
    )
    gh, ga = m["score"]
    return f'<span class="score">{gh}:{ga}{e(note)}</span>'


def round_table(ms: list[dict], published: dict, odds_cols: bool = True) -> str:
    """Tabulka kola: zápasy v řádcích, trhy 1/10/02/2 ve sloupcích.

    Kurzy neodehraných zápasů jsou klikací (skládají tiket), u odehraných
    se obarví vítězný/prohraný trh. S odds_cols=False jen los bez kurzů.
    """
    head = ""
    if odds_cols:
        head = (
            '<tr><th class="tname">Zápas</th><th></th>'
            + "".join(f'<th title="{MARKET_LABEL[mk]}">{mk}</th>' for mk in MARKETS)
            + "</tr>"
        )
    rows = []
    for m in ms:
        entry = published.get(str(m["id"]))
        odds = entry["odds"] if entry else {}
        outcome = reg_outcome(m)
        center = (
            score_html(m)
            if m["score"]
            else f'<span class="mtime">{e(m["time"] if m["time"] and m["time"] != "00:00" else "—")}</span>'
        )
        label = f'{m["home"]} – {m["away"]}'
        cells = []
        for mk in MARKETS if odds_cols else ():
            if mk not in odds:
                cells.append('<td class="ocell empty">–</td>')
                continue
            cls = "ocell"
            attrs = ""
            if outcome:
                cls += " win" if outcome in WINS[mk] else " lost"
            elif not m["score"]:
                cls += " click"
                attrs = (
                    f' data-mid="{m["id"]}" data-mk="{mk}" data-odd="{odds[mk]:.2f}"'
                    f' data-label="{e(label)}"'
                )
            cells.append(f'<td class="{cls}"{attrs}>{odds[mk]:.2f}</td>')
        rows.append(
            f'<tr><td class="tname"><span class="mdate">{cz_date(m["date"])}</span> '
            f'{e(m["home"])} – {e(m["away"])}</td>'
            f"<td>{center}</td>" + "".join(cells) + "</tr>"
        )
    return (
        f'<div class="scrollx"><table class="odds">{head}{"".join(rows)}</table></div>'
    )


def main() -> None:
    season = json.load(open(DATA / "season.json", encoding="utf-8"))
    pub_path = DATA / "published.json"
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    matches = season["matches"]
    by_round: dict[int, list[dict]] = collections.defaultdict(list)
    for m in matches:
        if m["round"]:
            by_round[m["round"]].append(m)
    for ms in by_round.values():
        ms.sort(key=lambda m: (m["date"] or "9999", m["time"] or "99"))

    state = settle(matches, published)
    for w in state["warnings"]:
        print("⚠", w)

    published_rounds = sorted({v["round"] for v in published.values()})
    open_round = next(
        (r for r in published_rounds if any(not m["score"] for m in by_round[r])), None
    )
    settled_rounds = [r for r in published_rounds if r != open_round]

    our_line = '<p class="ourmatch">⭐ Na náš zápas lze sázet jen výhru</p>'

    # ---------- záložka Divize Sázky ----------
    sazky = []

    # banky
    if state["banks"]:
        rows = "".join(
            f'<tr><td>{i}.</td><td class="tname">{e(p)}</td><td>{b:.0f}</td>'
            f'<td class="{"plus" if b >= START_BANK else "minus"}">{b - START_BANK:+.0f}</td></tr>'
            for i, (p, b) in enumerate(
                sorted(state["banks"].items(), key=lambda x: -x[1]), 1
            )
        )
        sazky.append(
            "<h2>Banky</h2><table><tr><th>#</th><th class='tname'>Sázkař</th>"
            f"<th>Bank</th><th>±</th></tr>{rows}</table>"
            f'<p class="note">Každý začíná s bankem {START_BANK}. Sázky hlaš bookmakerovi.</p>'
        )
    else:
        sazky.append(
            f'<h2>Banky</h2><p class="note">Zatím nikdo nesází. Každý začíná s bankem {START_BANK} — '
            "první tiket zakládá účet.</p>"
        )

    # vypsané kolo
    if open_round:
        ms = by_round[open_round]
        dates = sorted({m["date"] for m in ms if m["date"]})
        span = cz_date(dates[0]) + (
            f" – {cz_date(dates[-1])}" if len(dates) > 1 else ""
        )
        sazky.append(
            f"<h2>Vypsané kolo: {open_round}. kolo <span class='hspan'>{span}</span></h2>"
        )
        sazky.append(round_table(ms, published))
        commits_path = DATA / "commitments.json"
        commits = json.loads(commits_path.read_text()) if commits_path.exists() else []
        sealed_notes = [
            f'{e(c["person"])} <span class="hash">#{c["hash"][:8]}</span>'
            for c in commits
            if c["round"] == open_round and "revealed" not in c
        ]
        per = collections.Counter(
            t["person"] for t in state["open"].get(open_round, [])
        )
        sealed_notes += [f"{e(p)} ({n})" for p, n in sorted(per.items())]
        if sealed_notes:
            sazky.append(
                '<p class="note">🔒 Zapečetěné tikety (odhalí se po dohrání kola): '
                + ", ".join(sealed_notes)
                + "</p>"
            )

    # příští kolo
    future = [r for r in sorted(by_round) if r not in published_rounds]
    if future:
        nxt = future[0]
        sazky.append(
            f"<h2>Příští kolo: {nxt}. kolo</h2>"
            + round_table(by_round[nxt], published, odds_cols=False)
            + f'<p class="note">Kurzy vypíšeme po dohrání {open_round or nxt - 1}. kola — '
            "podle formy a výsledků.</p>"
        )

    # historie vypořádaných kol
    if settled_rounds:
        sazky.append("<h2>Odehraná kola</h2>")
        for rnd in reversed(settled_rounds):
            bet_rows = ""
            for t in state["settled"].get(rnd, []):
                legs = "<br>".join(
                    f'{e(leg["match"]["home"])} – {e(leg["match"]["away"])} '
                    f'<b>{leg["market"]}</b> @{leg["odd"]:.2f} {"✓" if win else "✗"}'
                    for leg, win in zip(t["legs"], t["leg_wins"])
                )
                kind = "AKO" if len(t["legs"]) > 1 else "sólo"
                bet_rows += (
                    f'<tr class="{"plus" if t["won"] else "minus"}"><td>{e(t["person"])}</td>'
                    f'<td class="tname">{legs}</td><td>{kind}</td><td>{t["odd"]:.2f}</td>'
                    f'<td>{t["stake"]:.0f}</td>'
                    f'<td>{"✅ " if t["won"] else "❌ "}{t["delta"]:+.0f}</td></tr>'
                )
            bets_html = (
                f'<table class="bets"><tr><th>Sázkař</th><th class="tname">Tiket</th><th>Typ</th>'
                f"<th>Kurz</th><th>Vklad</th><th>Výsledek</th></tr>{bet_rows}</table>"
                if bet_rows
                else '<p class="note">Bez tiketů.</p>'
            )
            sazky.append(
                f'<details class="round"><summary>{rnd}. kolo</summary>'
                f"{round_table(by_round[rnd], published)}{bets_html}</details>"
            )

    # ---------- záložka Los a tabulka ----------
    tab_rows = "".join(
        f'<tr class="{"us" if r["team"] == OUR_TEAM else ""}"><td>{i}.</td><td class="tname">{e(r["team"])}</td>'
        f'<td>{r["z"]}</td><td>{r["v"]}</td><td>{r["vp"]}</td><td>{r["pp"]}</td><td>{r["p"]}</td>'
        f'<td>{r["gf"]}:{r["ga"]}</td><td><b>{r["b"]}</b></td></tr>'
        for i, r in enumerate(standings(matches), 1)
    )
    los = [
        "<h2>Tabulka</h2>",
        f"<table><tr><th>#</th><th class='tname'>Tým</th><th>Z</th><th>V</th><th>VP</th>"
        f"<th>PP</th><th>P</th><th>Skóre</th><th>B</th></tr>{tab_rows}</table>",
        "<h2>Los</h2>",
    ]
    for rnd in sorted(by_round):
        ms = by_round[rnd]
        dates = sorted({m["date"] for m in ms if m["date"]})
        span = cz_date(dates[0]) + (
            f" – {cz_date(dates[-1])}" if len(dates) > 1 else ""
        )
        los.append(
            f'<details class="round"{" open" if rnd == open_round else ""}><summary>{rnd}. kolo '
            f'<span class="rspan">{span}</span></summary>'
            f"{round_table(ms, published, odds_cols=False)}</details>"
        )

    # ---------- záložka Tikety (vyhodnocené, s filtry) ----------
    ticket_rows = ""
    persons = sorted(state["banks"])
    for rnd in sorted(state["settled"], reverse=True):
        for t in state["settled"][rnd]:
            legs = "<br>".join(
                f'{e(leg["match"]["home"])} – {e(leg["match"]["away"])} '
                f'<b>{leg["market"]}</b> @{leg["odd"]:.2f} {"✓" if win else "✗"}'
                for leg, win in zip(t["legs"], t["leg_wins"])
            )
            ticket_rows += (
                f'<tr class="{"plus" if t["won"] else "minus"}" data-person="{e(t["person"])}"'
                f' data-won="{"win" if t["won"] else "lost"}" data-round="{rnd}"'
                f' data-delta="{t["delta"]:.0f}">'
                f'<td>{rnd}.</td><td>{e(t["person"])}</td><td class="tname">{legs}</td>'
                f'<td>{"AKO" if len(t["legs"]) > 1 else "sólo"}</td><td>{t["odd"]:.2f}</td>'
                f'<td>{t["stake"]:.0f}</td>'
                f'<td>{"✅ " if t["won"] else "❌ "}{t["delta"]:+.0f}</td></tr>'
            )
    if ticket_rows:
        person_chips = '<span class="fchip active" data-f="">Všichni</span>' + "".join(
            f'<span class="fchip" data-f="{e(p)}">{e(p)}</span>' for p in persons
        )
        round_opts = '<option value="">Všechna kola</option>' + "".join(
            f'<option value="{r}">{r}. kolo</option>'
            for r in sorted(state["settled"], reverse=True)
        )
        tikety = f"""<h2>Vyhodnocené tikety</h2>
<div class="filters">
  <div class="fgroup" data-key="person">{person_chips}</div>
  <div class="fgroup" data-key="result">
    <span class="fchip active" data-f="">Vše</span>
    <span class="fchip" data-f="win">✅ Výherní</span>
    <span class="fchip" data-f="lost">❌ Proherní</span>
  </div>
  <select id="f-round">{round_opts}</select>
</div>
<p class="note" id="tikety-sum"></p>
<div class="scrollx"><table class="bets" id="tickets-table">
<tr><th>Kolo</th><th>Sázkař</th><th class="tname">Tiket</th><th>Typ</th>
<th>Kurz</th><th>Vklad</th><th>Výsledek</th></tr>{ticket_rows}</table></div>"""
    else:
        tikety = (
            "<h2>Vyhodnocené tikety</h2>"
            '<p class="note">Zatím žádné — objeví se po dohrání prvního kola.</p>'
        )

    pub_key_path = DATA / "public_key.txt"
    pubkey = pub_key_path.read_text().strip() if pub_key_path.exists() else ""
    generated = datetime.datetime.now().strftime("%d. %m. %Y %H:%M")
    page = f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tipdivize — Divize B 2026/27</title>
<style>
:root {{
  --bg:#0e1512; --card:#16211b; --card2:#1c2a22; --line:#27382e;
  --text:#e8f0ea; --muted:#8fa697; --accent:#4ade80; --accent2:#facc15;
  --lost:#5b6b60; --red:#f87171;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:880px; margin:0 auto; padding:16px 12px 60px; }}
header h1 {{ font-size:30px; margin:18px 0 2px; letter-spacing:1px; }}
header h1 b {{ color:var(--accent); }}
header p {{ margin:0 0 4px; color:var(--muted); }}
nav {{ display:flex; gap:8px; margin:16px 0 4px; }}
nav a {{ padding:8px 18px; border-radius:10px 10px 0 0; background:var(--card);
  border:1px solid var(--line); border-bottom:none; color:var(--muted);
  text-decoration:none; font-weight:700; }}
nav a.active {{ background:var(--card2); color:var(--accent); }}
section.tab {{ display:none; }} section.tab.active {{ display:block; }}
h2 {{ font-size:18px; margin:26px 0 10px; color:var(--accent); }}
.hspan {{ color:var(--muted); font-weight:400; font-size:14px; }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border-radius:10px; overflow:hidden; font-size:14px; }}
th,td {{ padding:6px 8px; text-align:center; border-bottom:1px solid var(--line); }}
td.tname, th.tname {{ text-align:left; }}
th {{ color:var(--muted); font-weight:600; background:var(--card2); }}
tr.us td {{ background:rgba(74,222,128,.09); }}
tr:last-child td {{ border-bottom:none; }}
td.plus, tr.plus td:last-child {{ color:var(--accent); }}
td.minus, tr.minus td:last-child {{ color:var(--red); }}
.mdate {{ color:var(--muted); font-size:13px; margin-right:6px; }}
.mtime {{ color:var(--muted); font-size:13px; }}
.score {{ font-weight:800; color:var(--accent2); }}
.hash {{ font-family:monospace; font-size:12px; color:var(--muted); }}
.ourmatch {{ margin:8px 0 0; color:var(--accent); font-weight:700; }}
.scrollx {{ overflow-x:auto; }}
table.odds {{ margin:10px 0; }}
table.odds th {{ min-width:52px; }}
table.odds td.tname .mdate {{ margin-right:6px; }}
td.ocell {{ font-weight:700; }}
td.ocell.empty {{ color:var(--lost); font-weight:400; }}
td.ocell.click {{ cursor:pointer; }}
td.ocell.click:hover {{ color:var(--accent); }}
td.ocell.sel {{ background:rgba(250,204,21,.15); color:var(--accent2); }}
td.ocell.win {{ color:var(--accent); }}
td.ocell.lost {{ color:var(--lost); }}
#tbar {{ position:fixed; right:18px; bottom:18px; width:330px; max-height:80vh;
  overflow-y:auto; background:var(--card2); border:1px solid var(--line);
  border-radius:14px; display:none; z-index:9;
  box-shadow:0 12px 34px rgba(0,0,0,.55); }}
#tbar.on {{ display:block; }}
.slip-head {{ background:var(--accent); color:#08120c; font-weight:800;
  padding:9px 14px; font-size:15px; letter-spacing:.3px; }}
.slip-leg {{ display:flex; justify-content:space-between; align-items:center;
  gap:10px; padding:9px 14px; border-bottom:1px solid var(--line); }}
.slip-match {{ display:block; font-size:13px; line-height:1.3; }}
.slip-mk {{ color:var(--muted); font-size:12px; }}
.slip-odd {{ color:var(--accent2); font-weight:800; white-space:nowrap; }}
.slip-x {{ cursor:pointer; color:var(--muted); font-weight:700; padding:0 2px 0 8px; }}
.slip-x:hover {{ color:var(--red); }}
.slip-row {{ display:flex; justify-content:space-between; align-items:center;
  padding:8px 14px; font-size:14px; }}
.slip-row b {{ font-size:16px; }}
.slip-row.winrow b {{ color:var(--accent); }}
.slip-row.total {{ border-bottom:1px solid var(--line); }}
#tbar input {{ width:110px; background:var(--bg); color:var(--text); text-align:right;
  border:1px solid var(--line); border-radius:8px; padding:6px 8px; font-size:15px; }}
#tbar button {{ background:var(--accent); color:#08120c; font-weight:800; border:none;
  border-radius:10px; padding:9px 16px; cursor:pointer; font-size:15px;
  display:block; width:calc(100% - 28px); margin:8px 14px 14px; }}
#tbar button:hover {{ filter:brightness(1.1); }}
#tout {{ padding:0 14px 6px; }}
#tout textarea {{ width:100%; background:var(--bg); color:var(--accent2); box-sizing:border-box;
  border:1px solid var(--line); border-radius:8px; padding:6px; font:12px monospace; }}
#tout button {{ margin:8px 0 6px; width:100%; }}
.slipnote {{ color:var(--muted); font-size:12px; margin:0 0 10px; text-align:center; }}
@media (max-width:700px) {{ #tbar {{ left:12px; right:12px; bottom:12px; width:auto; }} }}
details.round {{ background:var(--card); border:1px solid var(--line);
  border-radius:12px; margin:10px 0; overflow:hidden; }}
details.round summary {{ cursor:pointer; padding:10px 14px; font-weight:700;
  background:var(--card2); list-style:none; display:flex; justify-content:space-between; }}
details.round summary::-webkit-details-marker {{ display:none; }}
details.round table.bets {{ border-radius:0; }}
details.round p.note {{ padding:0 14px; }}
details.round .scrollx table {{ margin:0; border-radius:0; }}
.rspan {{ color:var(--muted); font-weight:400; }}
p.note {{ color:var(--muted); font-size:13px; }}
.filters {{ display:flex; gap:14px; flex-wrap:wrap; align-items:center; margin:14px 0 8px; }}
.fgroup {{ display:flex; gap:6px; flex-wrap:wrap; }}
.fchip {{ background:var(--card2); border:1px solid var(--line); border-radius:20px;
  padding:4px 13px; font-size:13px; cursor:pointer; color:var(--muted); user-select:none; }}
.fchip:hover {{ border-color:var(--accent); }}
.fchip.active {{ border-color:var(--accent); color:var(--accent); font-weight:700; }}
#f-round {{ background:var(--card2); color:var(--text); border:1px solid var(--line);
  border-radius:20px; padding:5px 10px; font-size:13px; }}
footer {{ margin-top:34px; color:var(--muted); font-size:13px; }}
footer a {{ color:var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>TIP<b>DIVIZE</b></h1>
  <p>Florbal · Divize mužů, skupina B · sezóna 2026/2027</p>
  {our_line}
</header>

<nav>
  <a href="#sazky" id="nav-sazky">Divize Sázky</a>
  <a href="#tikety" id="nav-tikety">Tikety</a>
  <a href="#los" id="nav-los">Los a tabulka</a>
</nav>

<section class="tab" id="tab-sazky">{''.join(sazky)}
<footer>Zdroj dat: <a href="{season['url']}">ceskyflorbal.cz</a> · vygenerováno {generated}</footer>
</section>
<section class="tab" id="tab-tikety">{tikety}</section>
<section class="tab" id="tab-los">{''.join(los)}</section>
</div>

<div id="tbar">
  <div class="slip-head">🎫 TIKET</div>
  <div id="tlegs"></div>
  <div class="slip-row total"><span>Celkový kurz</span><b id="ttotal">–</b></div>
  <div class="slip-row"><span>Vklad</span><input id="tstake" type="number" min="1" placeholder="100"></div>
  <div class="slip-row winrow"><span>Možná výhra</span><b id="twin">–</b></div>
  <button id="tseal">🔒 Zapečetit tiket</button>
  <div id="tout" style="display:none">
    <textarea id="tcode" rows="3" readonly></textarea>
    <button id="tcopy">Zkopírovat</button>
    <p class="slipnote">Kód pošli do skupiny na Telegramu.</p>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/tweetnacl/1.0.3/nacl.min.js"></script>
<script>
var PUBKEY = "{pubkey}";
var MKLABEL = {{ "1": "výhra domácích", "10": "neprohra domácích",
  "02": "neprohra hostů", "2": "výhra hostů" }};
var sel = {{}};
function totalOdd() {{
  var total = 1;
  Object.keys(sel).forEach(function (mid) {{ total *= parseFloat(sel[mid].odd); }});
  return total;
}}
function renderWin() {{
  var stake = parseInt(document.getElementById("tstake").value, 10);
  document.getElementById("twin").textContent =
    stake > 0 ? Math.round(stake * totalOdd()) : "–";
}}
function renderBar() {{
  var bar = document.getElementById("tbar");
  var mids = Object.keys(sel);
  if (!mids.length) {{ bar.classList.remove("on"); return; }}
  var rows = "";
  mids.forEach(function (mid) {{
    var s = sel[mid];
    rows += '<div class="slip-leg"><div>' +
      '<span class="slip-match">' + s.label + '</span>' +
      '<span class="slip-mk">tip ' + s.mk + ' · ' + MKLABEL[s.mk] + '</span></div>' +
      '<div><span class="slip-odd">' + parseFloat(s.odd).toFixed(2) + '</span>' +
      '<span class="slip-x" data-x="' + mid + '" title="odebrat">×</span></div></div>';
  }});
  document.getElementById("tlegs").innerHTML = rows;
  document.getElementById("ttotal").textContent = totalOdd().toFixed(2);
  renderWin();
  document.getElementById("tout").style.display = "none";
  bar.classList.add("on");
}}
function removeLeg(mid) {{
  delete sel[mid];
  var cell = document.querySelector('.ocell.sel[data-mid="' + mid + '"]');
  if (cell) cell.classList.remove("sel");
  renderBar();
}}
document.getElementById("tlegs").addEventListener("click", function (ev) {{
  var mid = ev.target.dataset && ev.target.dataset.x;
  if (mid) removeLeg(mid);
}});
document.getElementById("tstake").addEventListener("input", renderWin);
document.querySelectorAll(".ocell.click").forEach(function (el) {{
  el.addEventListener("click", function () {{
    var mid = el.dataset.mid;
    var prev = document.querySelector('.ocell.sel[data-mid="' + mid + '"]');
    if (prev) prev.classList.remove("sel");
    if (sel[mid] && sel[mid].mk === el.dataset.mk) {{
      delete sel[mid];
    }} else {{
      sel[mid] = {{ mk: el.dataset.mk, odd: el.dataset.odd, label: el.dataset.label }};
      el.classList.add("sel");
    }}
    renderBar();
  }});
}});
document.getElementById("tseal").addEventListener("click", function () {{
  var stake = parseInt(document.getElementById("tstake").value, 10);
  if (!stake || stake <= 0) {{ alert("Zadej vklad."); return; }}
  if (!PUBKEY) {{ alert("Chybí veřejný klíč — bookmaker musí spustit keygen.py."); return; }}
  if (typeof nacl === "undefined") {{
    alert("Šifrovací knihovna se nenačetla — jsi online?"); return;
  }}
  var legs = Object.keys(sel).map(function (mid) {{
    return [parseInt(mid, 10), sel[mid].mk];
  }});
  var msg = new TextEncoder().encode(JSON.stringify({{ stake: stake, legs: legs }}));
  var pk = new Uint8Array(PUBKEY.match(/.{{2}}/g).map(function (h) {{
    return parseInt(h, 16);
  }}));
  var eph = nacl.box.keyPair();
  var nonce = nacl.randomBytes(24);
  var ct = nacl.box(msg, nonce, pk, eph.secretKey);
  var out = new Uint8Array(56 + ct.length);
  out.set(eph.publicKey, 0);
  out.set(nonce, 32);
  out.set(ct, 56);
  var code = "tip: " + btoa(String.fromCharCode.apply(null, out));
  var ta = document.getElementById("tcode");
  ta.value = code;
  document.getElementById("tout").style.display = "block";
  ta.select();
}});
document.getElementById("tcopy").addEventListener("click", function () {{
  var ta = document.getElementById("tcode");
  ta.select();
  try {{ navigator.clipboard.writeText(ta.value); }} catch (e) {{ document.execCommand("copy"); }}
  document.getElementById("tcopy").textContent = "Zkopírováno ✓";
}});
function showTab() {{
  var t = location.hash === "#los" ? "los"
    : location.hash === "#tikety" ? "tikety" : "sazky";
  document.querySelectorAll("section.tab, nav a").forEach(function (el) {{
    el.classList.remove("active");
  }});
  document.getElementById("tab-" + t).classList.add("active");
  document.getElementById("nav-" + t).classList.add("active");
}}
window.addEventListener("hashchange", showTab);
showTab();

if (document.getElementById("tickets-table")) {{
  var fState = {{ person: "", result: "", round: "" }};
  var applyF = function () {{
    var n = 0, sum = 0;
    document.querySelectorAll("#tickets-table tr[data-person]").forEach(function (r) {{
      var ok = (!fState.person || r.dataset.person === fState.person) &&
               (!fState.result || r.dataset.won === fState.result) &&
               (!fState.round || r.dataset.round === fState.round);
      r.style.display = ok ? "" : "none";
      if (ok) {{ n++; sum += parseFloat(r.dataset.delta); }}
    }});
    document.getElementById("tikety-sum").textContent = n
      ? "Tiketů: " + n + " · bilance " + (sum > 0 ? "+" : "") + Math.round(sum)
      : "Žádný tiket neodpovídá filtru.";
  }};
  document.querySelectorAll(".fchip").forEach(function (ch) {{
    ch.addEventListener("click", function () {{
      var g = ch.parentElement;
      g.querySelectorAll(".fchip").forEach(function (x) {{ x.classList.remove("active"); }});
      ch.classList.add("active");
      fState[g.dataset.key] = ch.dataset.f;
      applyF();
    }});
  }});
  document.getElementById("f-round").addEventListener("change", function (ev) {{
    fState.round = ev.target.value;
    applyF();
  }});
  applyF();
}}
</script>
</body>
</html>
"""
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(
        f"index.html vygenerován ({len(page) // 1024} kB), "
        f"vypsané kolo: {open_round}, sázkařů: {len(state['banks'])}"
    )


if __name__ == "__main__":
    main()
