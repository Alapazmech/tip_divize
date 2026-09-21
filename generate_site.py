"""Generuje statický index.html: záložky „Divize Sázky", „Banky", „Tikety", „Los a tabulka" a „Informace".

Vstupy: data/season.json (los + výsledky), data/published.json (zmrazené
kurzy), data/bets.csv (tikety). Vypořádání: trhy 1/10/0/02/2 se vztahují
k ZÁKLADNÍ HRACÍ DOBĚ (prodloužení/nájezdy = remíza v základní době).
Na zápasy Bohemians jsou vypsané jen výhry (1 a 2) — žádné zajišťování.

Sázky v bets.csv: round,person,ticket,match,market,stake — `match` je id
zápasu, nebo jednoznačný kus jména týmu v daném kole (např. „Olymp").
Řádky se stejným (round, person, ticket) tvoří jeden AKO tiket: kurzy legů
se násobí a vyjít musí všechny; vklad platí ten z prvního řádku tiketu.
Prázdný `ticket` = sólo tiket.

Dokupy v data/topups.csv: round,person,credits,paid — když někdo prohraje
všechno, zaplatí dalších 100 Kč (`paid`) a dostane `credits` kreditů do banku
(vždy 100, kolikrát chce). Bank se připíše hned; `round` říká, od kterého
kola dokup figuruje v grafu a historii.
Řádky zapisuje bot na /dokoupit (tickets.dokoupit), ručně jde taky.
"""

import collections
import csv
import datetime
import html
import json
import math
import pathlib
import unicodedata

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"

OUR_TEAM = "FbŠ Florbal Bohemians"
START_BANK = 100  # 100 Kč reálného vkladu = 100 kreditů
BUYIN_KC = 100  # skutečná cena vstupu i každého dokupu
MARKETS = ("1", "10", "0", "02", "2")
MARKET_LABEL = {
    "1": "výhra domácích",
    "10": "neprohra domácích",
    "0": "remíza v základní době",
    "02": "neprohra hostů",
    "2": "výhra hostů",
}
WINS = {"1": {"1"}, "10": {"1", "0"}, "0": {"0"}, "02": {"0", "2"}, "2": {"2"}}

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


def dohravka_bettable(m: dict, next_matches: list[dict]) -> bool:
    """Odložený zápas ze staršího kola se sází spolu s aktuálním kolem, když se
    hraje dřív než první zápas PŘÍŠTÍHO kola (cokoliv před 3. kolem patří do
    okna 2. kola). Bez známého termínu se nesází; když příští kolo termíny
    nemá, sází se."""
    if not m.get("date"):
        return False
    dates = [x["date"] for x in next_matches if x["date"]]
    return not dates or m["date"] < min(dates)


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


def settle(
    matches: list[dict], published: dict, bets_path: pathlib.Path | None = None
) -> dict:
    """Projde kola chronologicky, vypořádá tikety -> stav bank."""
    by_round: dict[int, list[dict]] = collections.defaultdict(list)
    for m in matches:
        if m["round"]:
            by_round[m["round"]].append(m)

    bets_path = bets_path or DATA / "bets.csv"
    bets = load_csv(bets_path)
    # dokupy leží vedle tiketů: bets.csv -> topups.csv, demo_bets.csv -> demo_topups.csv
    topups_path = bets_path.with_name(bets_path.name.replace("bets", "topups"))
    topups: dict[int, list[dict]] = collections.defaultdict(list)
    for row in load_csv(topups_path) if topups_path.exists() else []:
        topups[int(row["round"])].append(
            {
                "person": row["person"].strip(),
                "credits": float(row["credits"]),
                "paid": float(row.get("paid") or BUYIN_KC),
            }
        )
    banks: dict[str, float] = {}
    # kolik kdo do hry vložil: kredity (start + dokupy) a skutečné koruny
    deposits: dict[str, dict] = {}

    def join(person: str) -> None:
        if person not in banks:
            banks[person] = START_BANK
            deposits[person] = {"credits": START_BANK, "kc": BUYIN_KC, "topups": 0}

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
            if not match and b["match"].strip().isdigit():
                # dohrávka ze staršího kola vsazená spolu s aktuálním kolem
                match = resolve_match(b["match"], matches)
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
                "label": label,
                "legs": legs,
                "stake": float(rows[0]["stake"]),
                "odd": round(total_odd, 2),
            }
        )
        join(person)
    for rows in topups.values():
        for tu in rows:
            join(tu["person"])

    history: list[dict] = []  # vývoj banků: snapshot po každém vypořádaném kole
    for rnd in sorted(by_round):
        # dokupy se počítají od začátku kola — dřív než se vypořádají jeho tikety
        # (bank hráče je tak připsaný hned, i když se kolo teprve hraje)
        for tu in topups.get(rnd, []):
            banks[tu["person"]] += tu["credits"]
            deposits[tu["person"]]["credits"] += tu["credits"]
            deposits[tu["person"]]["kc"] += tu["paid"]
            deposits[tu["person"]]["topups"] += 1
        stakes_this_round: dict[str, float] = collections.defaultdict(float)
        for t in [x for x in tickets if x["round"] == rnd]:
            outcomes = [reg_outcome(leg["match"]) for leg in t["legs"]]
            if any(o is None for o in outcomes):
                open_rows[rnd].append(t)
                stakes_this_round[t["person"]] += t["stake"]
                continue
            leg_wins = [o in WINS[leg["market"]] for o, leg in zip(outcomes, t["legs"])]
            won = all(leg_wins)
            # čistá výhra na desetiny kreditu (bez float šumu typu 57.4999)
            delta = round(t["stake"] * (t["odd"] - 1), 1) if won else -t["stake"]
            banks[t["person"]] += delta
            settled_rows[rnd].append(
                {**t, "won": won, "delta": delta, "leg_wins": leg_wins}
            )
        for person, staked in stakes_this_round.items():
            if staked > banks[person]:
                warnings.append(
                    f"{person} má v {rnd}. kole vsazeno {kr(staked)}, ale bank je {kr(banks[person])}"
                )
        if settled_rows.get(rnd):
            history.append({"round": rnd, "banks": dict(banks)})

    return {
        "banks": banks,
        "deposits": deposits,
        "pot_kc": sum(d["kc"] for d in deposits.values()),
        "history": history,
        "settled": settled_rows,
        "open": open_rows,
        "warnings": warnings,
    }


def kr(x: float, sign: bool = False) -> str:
    """Kredity: celé číslo bez desetin, jinak na jednu desetinu (57.5)."""
    s = f"{x:+.1f}" if sign else f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def person_stats(state: dict) -> dict[str, dict]:
    """Statistika sázkaře z vypořádaných tiketů: počet, úspěšnost, ROI, nejvyšší výhra."""
    stats: dict[str, dict] = {}
    for p in state["banks"]:
        stats[p] = {"tickets": 0, "wins": 0, "staked": 0.0, "profit": 0.0, "best": 0.0}
    for rows in state["settled"].values():
        for t in rows:
            st = stats[t["person"]]
            st["tickets"] += 1
            st["wins"] += t["won"]
            st["staked"] += t["stake"]
            st["profit"] += t["delta"]
            st["best"] = max(st["best"], t["delta"])
    for st in stats.values():
        st["hit"] = st["wins"] / st["tickets"] if st["tickets"] else None
        st["roi"] = st["profit"] / st["staked"] if st["staked"] else None
    return stats


# kategorická paleta pro graf (validovaná na tmavém podkladu karty), pořadí pevné
CHART_COLORS = (
    "#3987e5", "#d95926", "#199e70", "#c98500",
    "#d55181", "#008300", "#9085e9", "#e66767",
)


def payout_now(state: dict) -> dict[str, float]:
    """Kdyby základní část skončila teď: první dva si dělí bank v poměru svých banků."""
    pot = state["pot_kc"]
    top = sorted(state["banks"].items(), key=lambda x: -x[1])[:2]
    total = sum(b for _, b in top)
    if not pot or len(top) < 2 or total <= 0:
        return {}
    return {p: pot * b / total for p, b in top}


def pot_note(state: dict) -> str:
    """Věta pod tabulkou banků: kolik je reálně ve hře a jak by se to teď dělilo."""
    pot = state["pot_kc"]
    if not pot:
        return ""
    n = len(state["deposits"])
    topups_kc = sum(d["kc"] - BUYIN_KC for d in state["deposits"].values())
    text = f"V banku je <b>{pot:.0f} Kč</b> ({n} × {BUYIN_KC} Kč"
    text += f" + dokupy {topups_kc:.0f} Kč)." if topups_kc else ")."
    top = sorted(state["banks"].items(), key=lambda x: -x[1])[:2]
    if len(top) == 2 and top[0][1] + top[1][1] > 0:
        total = top[0][1] + top[1][1]
        share = " a ".join(
            f"{e(p)} {b / total * 100:.0f} % = {pot * b / total:.0f} Kč" for p, b in top
        )
        text += f" Kdyby základní část skončila teď, berou první dva: {share}."
    return f'<p class="note">{text}</p>'


def info_tab() -> str:
    """Záložka Informace: pravidla hry, jak sázet, příkazy bota, emoji."""
    return f"""<h2>Pravidla</h2>
<ul class="rules">
<li><b>Vklad {BUYIN_KC} Kč = bank {START_BANK} kreditů.</b> 1 kredit = 1 Kč. Peníze se vybírají a vyplácejí až na konci.</li>
<li><b>Hraje se jen základní část</b> (22 kol), na play-off se nesází.</li>
<li><b>Sázej, jak chceš.</b> Sólo i AKO, klidně celý bank. Jen na právě vypsané kolo, do začátku zápasu. Dohrávky hrané před dalším kolem se sází spolu s ním.</li>
<li><b>Vše je vidět.</b> Živý tiket je tajný (na stránce jen 🔒 otisk). Po dohrání kola se odhalí a vyhodnotí — všechny jsou v záložce <a href="#tikety">Tikety</a>.</li>
<li><b>Bank 0? Dokup.</b> Dalších {BUYIN_KC} Kč = dalších <b>{START_BANK} kreditů</b>, kdykoliv a kolikrát chceš. Napiš <code>/dokoupit</code>.</li>
<li><b>Na konci berou první dva vše</b>, v poměru svých banků. Pavel 3000 a Jan 1000 → Pavel ¾, Jan ¼.</li>
</ul>

<h2>Jak vsadit</h2>
<ol class="rules">
<li>Klikni na kurzy v <a href="#sazky">Divize Sázky</a>.</li>
<li>Zadej vklad, klikni <b>🔒 Zapečetit tiket</b>, zkopíruj kód <code>tip: …</code>.</li>
<li>Kód pošli do Telegram skupiny. Bot dá ✅ = tiket je podaný.</li>
</ol>
<p class="note"><b>1</b> výhra domácích · <b>10</b> neprohra domácích · <b>0</b> remíza · <b>02</b> neprohra hostů · <b>2</b> výhra hostů.
Počítá se základní hrací doba (prodloužení = remíza). Bohemians: jen výhra Bohemky.</p>

<h2>Bot v chatu</h2>
<ul class="rules">
<li><code>tip: …</code> — podá tiket.</li>
<li><code>/banky</code> — stav banků.</li>
<li><code>/vysledky</code> — vyhodnocení posledního kola.</li>
<li><code>/dokoupit</code> — dokup při banku 0.</li>
</ul>

<h2>Emoji</h2>
<ul class="rules">
<li>✅ reakce na tvůj kód — tiket podaný. Bez reakce a s odpovědí — tiket nepodaný, bot napíše proč.</li>
<li>Po kole: ✅ tiket vyhrál, ❌ prohrál, ⏳ čeká na dohrávku.</li>
</ul>
"""


def bank_chart(history: list[dict], persons: list[str]) -> str:
    """Inline SVG: vývoj banku každého sázkaře po kolech (start = START_BANK)."""
    if not history or not persons:
        return ""
    if len(persons) > len(CHART_COLORS):
        # víc než 8 sázkařů: graf jen pro 8 s nejvyšším bankem, zbytek v tabulce
        persons = sorted(persons, key=lambda p: -history[-1]["banks"].get(p, 0))[:8]
    color = {p: CHART_COLORS[i] for i, p in enumerate(sorted(persons))}
    rounds = [0] + [h["round"] for h in history]
    series = {
        p: [START_BANK] + [h["banks"].get(p, START_BANK) for h in history]
        for p in persons
    }
    vals = [v for vs in series.values() for v in vs]
    vmin, vmax = min(vals), max(vals)
    # hezké kroky osy: nejmenší krok, při kterém vyjde nejvýš 6 linek
    step = next(
        st for st in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000)
        if (vmax - vmin) / st <= 5
    )
    lo = math.floor(vmin / step) * step
    hi = math.ceil(vmax / step) * step
    if hi == lo:
        hi = lo + step
    ticks = [lo + step * k for k in range(int((hi - lo) / step) + 1)]
    W, H, L, R, T, B = 640, 260, 46, 110, 14, 30
    px = lambda i: L + (W - L - R) * i / max(len(rounds) - 1, 1)
    py = lambda v: T + (H - T - B) * (hi - v) / (hi - lo)
    out = [
        f'<svg class="bankchart" viewBox="0 0 {W} {H}" role="img" '
        'aria-label="Vývoj banků po kolech">'
    ]
    # mřížka na hezkých hodnotách + start 1000 čárkovaně
    for v in ticks:
        y = py(v)
        out.append(
            f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{L - 6}" y="{y + 4:.1f}" class="ax" text-anchor="end">{v:.0f}</text>'
        )
    if lo < START_BANK < hi:
        y = py(START_BANK)
        out.append(
            f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="base"/>'
        )
    for i, r in enumerate(rounds):
        out.append(
            f'<text x="{px(i):.1f}" y="{H - 10}" class="ax" text-anchor="middle">'
            f'{"start" if r == 0 else f"{r}."}</text>'
        )
    # čáry, body s tooltipem, přímé popisky na konci (barva + text = identita nejen barvou)
    ends = sorted(persons, key=lambda p: -series[p][-1])
    used_y: list[float] = []
    for p in ends:
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(series[p]))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color[p]}" stroke-width="2"/>')
        for i, v in enumerate(series[p]):
            r = rounds[i]
            tip = f"{p} · {'start' if r == 0 else f'{r}. kolo'} · bank {kr(v)}"
            out.append(
                f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="4" fill="{color[p]}" '
                f'stroke="var(--card)" stroke-width="2"><title>{e(tip)}</title></circle>'
            )
        y = py(series[p][-1])
        while any(abs(y - u) < 13 for u in used_y):
            y += 13
        used_y.append(y)
        out.append(
            f'<text x="{W - R + 8}" y="{y + 4:.1f}" class="lbl" fill="{color[p]}">'
            f'{e(p)} {kr(series[p][-1])}</text>'
        )
    out.append("</svg>")
    return "".join(out)


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


def round_table(
    ms: list[dict], published: dict, odds_cols: bool = True, clickable: bool = True
) -> str:
    """Tabulka kola: zápasy v řádcích, trhy 1/10/0/02/2 ve sloupcích.

    Kurzy neodehraných zápasů jsou klikací (skládají tiket), u odehraných
    se obarví vítězný/prohraný trh. S odds_cols=False jen los bez kurzů.
    `clickable` je bool, nebo predikát match -> bool (dohrávky po zápasech).
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
            elif (clickable(m) if callable(clickable) else clickable) and not m["score"]:
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


def betting_sections(
    matches: list[dict],
    published: dict,
    bets_path: pathlib.Path,
    commits_path: pathlib.Path,
    clickable: bool,
) -> tuple[list[str], list[str], dict, int | None, dict]:
    """Sázkové sekce: banky (vlastní záložka) a sázky (vypsané kolo, historie).

    Společné pro ostrou ligu i zkušební záložku; vrací (html banky, html
    sázky, stav vypořádání, vypsané kolo, zápasy po kolech).
    """
    by_round: dict[int, list[dict]] = collections.defaultdict(list)
    for m in matches:
        if m["round"]:
            by_round[m["round"]].append(m)
    for ms in by_round.values():
        ms.sort(key=lambda m: (m["date"] or "9999", m["time"] or "99"))

    state = settle(matches, published, bets_path)

    published_rounds = sorted({v["round"] for v in published.values()})
    open_rounds = [
        r for r in published_rounds if any(not m["score"] for m in by_round[r])
    ]
    open_round = open_rounds[0] if open_rounds else None
    settled_rounds = [r for r in published_rounds if r not in open_rounds]

    sazky: list[str] = []
    banky: list[str] = []

    # banky + statistika sázkařů (vlastní záložka)
    if state["banks"]:
        stats = person_stats(state)
        has_stats = any(st["tickets"] for st in stats.values())
        pct = lambda x: f"{x * 100:.0f} %" if x is not None else "–"
        payout = payout_now(state)
        rows = ""
        for i, (p, b) in enumerate(sorted(state["banks"].items(), key=lambda x: -x[1]), 1):
            st = stats[p]
            dep = state["deposits"][p]
            pay = payout.get(p)
            rows += (
                f'<tr><td>{i}.</td><td class="tname">{e(p)}</td><td><b>{kr(b)}</b></td>'
                f'<td class="{"plus" if b >= dep["credits"] else "minus"}" title="bank − vložené kredity">{kr(b - dep["credits"], sign=True)}</td>'
                + (f'<td class="plus"><b>{pay:.0f} Kč</b></td>' if pay else "<td>–</td>")
            )
            rows += f'<td title="dokupů · zaplaceno celkem">{dep["topups"]}× · {dep["kc"]:.0f} Kč</td>'
            if has_stats:
                roi_cls = "" if st["roi"] is None else ("plus" if st["roi"] >= 0 else "minus")
                rows += (
                    f'<td>{st["tickets"]}</td>'
                    f'<td title="výherních tiketů / všech">{st["wins"]}/{st["tickets"]} · {pct(st["hit"])}</td>'
                    f'<td>{st["staked"]:.0f}</td>'
                    f'<td class="{roi_cls}" title="čistý zisk / vsazeno">{pct(st["roi"])}</td>'
                    + (f'<td>{kr(st["best"], sign=True)}</td>' if st["best"] else "<td>–</td>")
                )
            rows += "</tr>"
        head = (
            "<th>#</th><th class='tname'>Sázkař</th><th>Bank</th><th title='bank − vložené kredity'>±</th>"
            "<th title='kdyby základní část skončila teď: první dva si dělí bank v poměru banků'>Bere teď</th>"
        )
        head += "<th title='počet dokupů · zaplaceno celkem'>Dokupy</th>"
        if has_stats:
            head += (
                "<th title='vypořádaných tiketů'>Tiketů</th><th title='výherních / všech'>Úspěšnost</th>"
                "<th>Vsazeno</th><th title='čistý zisk / vsazeno'>ROI</th><th title='nejvyšší čistá výhra na tiket'>Top výhra</th>"
            )
        banky.append(
            f'<h2>Banky</h2><div class="scrollx"><table class="stats"><tr>{head}</tr>{rows}</table></div>'
            + pot_note(state)
        )
        chart = bank_chart(state["history"], sorted(state["banks"]))
        if chart:
            banky.append(
                '<h3>Vývoj banků</h3><div class="chartwrap">' + chart + "</div>"
                '<p class="note">Bank po každém dohraném kole. Šedá linka = startovních '
                f"{START_BANK}. Najetím na bod uvidíš hodnotu.</p>"
            )
    else:
        banky.append(
            f'<h2>Banky</h2><p class="note">Zatím nikdo nesází. Každý vloží {BUYIN_KC} Kč a začíná s bankem {START_BANK} — '
            'první tiket zakládá účet. Pravidla jsou v záložce <a href="#info">Informace</a>.</p>'
        )

    # vypsaná kola (víc než jedno = čeká se na dohrávku; ta se sází spolu
    # s aktuálním kolem, pokud se hraje nejpozději s ním)
    latest_pub = published_rounds[-1] if published_rounds else None
    next_ms = by_round.get(latest_pub + 1, []) if latest_pub else []
    for rnd in sorted(open_rounds, key=lambda r: r != latest_pub):  # aktuální kolo první
        is_latest = rnd == latest_pub
        ms = (
            by_round[rnd] if is_latest else [m for m in by_round[rnd] if not m["score"]]
        )
        dates = sorted({m["date"] for m in ms if m["date"]})
        span = cz_date(dates[0]) + (
            f" – {cz_date(dates[-1])}" if len(dates) > 1 else ""
        )
        if is_latest:
            tag = ""
        elif any(dohravka_bettable(m, next_ms) for m in ms):
            tag = " · dohrávka — sází se spolu s aktuálním kolem"
        else:
            tag = " · dohrávka — sázky se otevřou s dalším kolem"
        sazky.append(
            f"<h2>Vypsané kolo: {rnd}. kolo <span class='hspan'>{span}{tag}</span></h2>"
        )
        can = clickable and (is_latest or (lambda m: dohravka_bettable(m, next_ms)))
        sazky.append(round_table(ms, published, clickable=can))
    if open_rounds:
        commits = json.loads(commits_path.read_text()) if commits_path.exists() else []
        # přehled po lidech: jméno + počet tiketů (otisky jen v tooltipu,
        # ověřit si je může kdo chce po odhalení)
        per: dict[str, dict] = {}
        for c in commits:
            if "revealed" in c:
                continue
            d = per.setdefault(c["person"], {"n": 0, "hashes": [], "wait": False})
            d["n"] += 1
            d["hashes"].append(c["hash"][:8])
            if latest_pub and c["round"] < latest_pub:
                d["wait"] = True
        for rows in state["open"].values():
            for t in rows:
                per.setdefault(t["person"], {"n": 0, "hashes": [], "wait": False})["n"] += 1
        if per:
            items = []
            for person, d in sorted(per.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
                n = d["n"]
                word = "tiket" if n == 1 else "tikety" if n < 5 else "tiketů"
                title = f' title="#{", #".join(d["hashes"])}"' if d["hashes"] else ""
                wait = " ⏳ čeká na dohrávku" if d["wait"] else ""
                items.append(f"<li{title}>{e(person)} — {n} {word}{wait}</li>")
            sazky.append(
                '<p class="note">🔒 Živé tikety — podané, odhalí se po dohrání zápasů:</p>'
                '<ul class="sealed">' + "".join(items) + "</ul>"
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
                    f'<td>{"✅ " if t["won"] else "❌ "}{kr(t["delta"], sign=True)}</td></tr>'
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

    return banky, sazky, state, open_round, by_round


def main() -> None:
    season = json.load(open(DATA / "season.json", encoding="utf-8"))
    pub_path = DATA / "published.json"
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    matches = season["matches"]

    demo_season_path = DATA / "demo_season.json"
    demo_active = (DATA / "demo_active").exists()

    banky, sazky, state, open_round, by_round = betting_sections(
        matches,
        published,
        DATA / "bets.csv",
        DATA / "commitments.json",
        clickable=not demo_active,  # během testu se ostré kurzy neklikají
    )
    for w in state["warnings"]:
        print("⚠", w)

    our_line = '<p class="ourmatch">⭐ Na náš zápas lze sázet jen výhru</p>'

    # ---------- dočasná zkušební záložka ----------
    demo_tab = ""
    if demo_season_path.exists():
        demo_season = json.load(open(demo_season_path, encoding="utf-8"))
        dpub_path = DATA / "demo_published.json"
        demo_pub = json.loads(dpub_path.read_text()) if dpub_path.exists() else {}
        demo_banky, demo_parts, demo_state, _, _ = betting_sections(
            demo_season["matches"],
            demo_pub,
            DATA / "demo_bets.csv",
            DATA / "demo_commitments.json",
            clickable=demo_active,
        )
        for w in demo_state["warnings"]:
            print("⚠ [test]", w)
        demo_tab = (
            '<p class="note">🧪 Zkušební liga na osahání sázení (zápasy Divize A, '
            "výsledky se losují). O nic nejde, banky jsou oddělené od ostré hry.</p>"
            + "".join(demo_banky + demo_parts)
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
                f' data-delta="{t["delta"]:.1f}">'
                f'<td>{rnd}.</td><td>{e(t["person"])}</td><td class="tname">{legs}</td>'
                f'<td>{"AKO" if len(t["legs"]) > 1 else "sólo"}</td><td>{t["odd"]:.2f}</td>'
                f'<td>{t["stake"]:.0f}</td>'
                f'<td>{"✅ " if t["won"] else "❌ "}{kr(t["delta"], sign=True)}</td></tr>'
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

    demo_nav = '<a href="#test" id="nav-test">🧪 Test</a>' if demo_tab else ""
    demo_section = (
        f'<section class="tab" id="tab-test">{demo_tab}</section>' if demo_tab else ""
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
h3 {{ color:var(--muted); font-size:15px; margin:18px 0 6px; font-weight:600; }}
.chartwrap {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:8px 6px; }}
.bankchart {{ width:100%; height:auto; display:block; font:11px system-ui,sans-serif; }}
.bankchart .grid {{ stroke:var(--line); stroke-width:1; }}
.bankchart .base {{ stroke:var(--muted); stroke-width:1; stroke-dasharray:4 4; }}
.bankchart .ax {{ fill:var(--muted); }}
.bankchart .lbl {{ font-weight:700; font-size:12px; }}
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
#tbar {{ position:fixed; right:18px; bottom:18px; width:290px; max-height:80vh;
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
/* otevřený tiket nesmí nic překrývat: na širokém displeji dostane vlastní
   sloupec vpravo od obsahu, na úzkém je dole a stránka pod ním dostane místo */
@media (min-width:1000px) {{
  body.slip-on .wrap {{ max-width:1200px; padding-right:320px; }} }}
@media (max-width:999px) {{
  #tbar {{ left:12px; right:12px; bottom:12px; width:auto; max-height:45vh; }}
  body.slip-on .wrap {{ padding-bottom:50vh; }} }}
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
ul.sealed {{ margin:-6px 0 14px; padding-left:22px; font-size:14px; }}
ul.rules, ol.rules {{ padding-left:22px; font-size:15px; line-height:1.55; max-width:720px; }}
ul.rules li, ol.rules li {{ margin:8px 0; }}
.rules code, p.note code {{ background:var(--card2); border:1px solid var(--line); border-radius:4px; padding:1px 5px; font-size:13px; }}
ul.sealed li {{ margin:2px 0; }}
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
  <a href="#banky" id="nav-banky">Banky</a>
  {demo_nav}
  <a href="#tikety" id="nav-tikety">Tikety</a>
  <a href="#los" id="nav-los">Los a tabulka</a>
  <a href="#info" id="nav-info">Informace</a>
</nav>

<section class="tab" id="tab-sazky">{''.join(sazky)}
<footer>Zdroj dat: <a href="{season['url']}">ceskyflorbal.cz</a> · vygenerováno {generated}</footer>
</section>
<section class="tab" id="tab-banky">{''.join(banky)}</section>
{demo_section}
<section class="tab" id="tab-tikety">{tikety}</section>
<section class="tab" id="tab-los">{''.join(los)}</section>
<section class="tab" id="tab-info">{info_tab()}</section>
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
    <p class="slipnote">Kód pošli do skupiny na Telegramu. Tiket je podaný, až bot dá ✅.</p>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/tweetnacl/1.0.3/nacl.min.js"></script>
<script>
var PUBKEY = "{pubkey}";
var MKLABEL = {{ "1": "výhra domácích", "10": "neprohra domácích",
  "0": "remíza v základní době", "02": "neprohra hostů", "2": "výhra hostů" }};
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
  document.body.classList.toggle("slip-on", mids.length > 0);
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
  var MKCODE = {{ "1": 0, "2": 1, "10": 2, "02": 3, "0": 4 }};
  var legs = Object.keys(sel).map(function (mid) {{
    return [parseInt(mid, 10), sel[mid].mk];
  }});
  // kompaktní binární payload: vklad uint24 | počet | (match_id uint32, trh u8)*
  var body = new Uint8Array(4 + legs.length * 5);
  body[0] = (stake >> 16) & 255; body[1] = (stake >> 8) & 255; body[2] = stake & 255;
  body[3] = legs.length;
  legs.forEach(function (l, i) {{
    var o = 4 + i * 5, id = l[0];
    body[o] = (id >>> 24) & 255; body[o + 1] = (id >>> 16) & 255;
    body[o + 2] = (id >>> 8) & 255; body[o + 3] = id & 255;
    body[o + 4] = MKCODE[l[1]];
  }});
  var pk = new Uint8Array(PUBKEY.match(/.{{2}}/g).map(function (h) {{
    return parseInt(h, 16);
  }}));
  var eph = nacl.box.keyPair();
  // nonce se neposílá — obě strany ho odvodí z klíčů
  var cat = new Uint8Array(64);
  cat.set(eph.publicKey, 0); cat.set(pk, 32);
  var nonce = nacl.hash(cat).slice(0, 24);
  var ct = nacl.box(body, nonce, pk, eph.secretKey);
  var out = new Uint8Array(32 + ct.length);
  out.set(eph.publicKey, 0);
  out.set(ct, 32);
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
  var t = location.hash.replace("#", "") || "sazky";
  if (!document.getElementById("tab-" + t)) t = "sazky";
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
      ? "Tiketů: " + n + " · bilance " + (sum > 0 ? "+" : "") + (Math.round(sum * 10) / 10)
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
