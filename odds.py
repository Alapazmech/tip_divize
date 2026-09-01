"""Kurzový model Tipdivize.

Síla týmů: Elo přes všechny loňské zápasy (Divize A–E + Národní liga, dedup
podle id zápasu), NL týmy startují výš (soutěž o úroveň výš), nováčci z krajů
níž. Před novou sezónou se ratingy stáhnou ke startovnímu průměru (regrese),
pak se průběžně aktualizují letošními výsledky — kurzy dalšího kola tak
odrážejí aktuální formu.

Góly: očekávaný rozdíl skóre se odvozuje z rozdílu Elo (koeficient beta se
fituje na loňských datech), celkový počet gólů z ligového průměru a tempa
obou týmů. Poissonova mřížka dává pravděpodobnosti výsledku ZÁKLADNÍ HRACÍ
DOBY — trhy jsou klasické 1 / 10 / 02 / 2 (výhra a neprohra v normální hrací
době; v Divizi se pak dohrává do rozhodnutí, ale to už kurzů netýká).

Vypisuje se vždy JEDNO CELÉ KOLO: to, ve kterém je nejbližší neodehraný
zápas. Jednou vypsaný kurz je zmrazený v data/published.json a nemění se.

Použití:
    python3 odds.py            # vypíše aktuální kolo (pokud už není vypsané)
    python3 odds.py --refresh  # přepočítá vypsané kurzy NEODEHRANÝCH zápasů
"""

import glob
import json
import math
import pathlib
import sys
import time

DATA = pathlib.Path(__file__).parent / "data"

OUR_TEAM = "FbŠ Florbal Bohemians"

ELO_K = 24
ELO_HOME_ADV = 35

# Expertní korekce bookmakera k Elo ze startu sezóny (znalost kádrů > loňská
# čísla). Aplikují se jednou na startovní rating; průběžné výsledky pak
# ratingy dál posouvají normálně.
EXPERT_ADJUST = {
    "FbC Plzeň": +30,  # o chlup lepší než Bohemians
    "T.B.C. Králův Dvůr": +40,  # slušný tým, ne podprůměr
    "OLYMP FLORBAL": -110,  # bude děsnej
    "Banes Florbal Soběslav": -60,  # nováček, bude děsnej
}
REGRESS = 0.30  # návrat ke startovnímu ratingu mezi sezónami
NEWCOMER_PRIOR = 1400  # postupující z krajského přeboru
MARGIN = 0.08  # marže kanceláře na trh
MIN_ODD, MAX_ODD = 1.02, 15.0
GRID = 25  # Poissonova mřížka 0..GRID gólů


def elo_expect(r_home: float, r_away: float, home_adv: float = ELO_HOME_ADV) -> float:
    return 1.0 / (1.0 + 10 ** (-(r_home + home_adv - r_away) / 400.0))


def load_history() -> list[dict]:
    seen: set[int] = set()
    matches = []
    for path in sorted(glob.glob(str(DATA / "history" / "*.json"))):
        blob = json.load(open(path, encoding="utf-8"))
        for m in blob["matches"]:
            if not m["score"] or m["id"] in seen:
                continue
            seen.add(m["id"])
            m["prior"] = blob["prior"]
            matches.append(m)
    matches.sort(key=lambda m: (m["date"] or "9999", m["id"]))
    return matches


class Model:
    def __init__(self) -> None:
        self.rating: dict[str, float] = {}
        self.start: dict[str, float] = {}
        self.pace: dict[str, list[int]] = {}  # celkové góly v zápasech týmu
        self.totals: list[int] = []
        self._diff_samples: list[tuple[float, int]] = []  # (elo diff, rozdíl gólů)
        self.beta = 1.0 / 130.0  # góly rozdílu na bod Elo, přefituje se
        self.home_goal_edge = 0.3

    def _ensure(self, team: str, prior: float) -> None:
        if team not in self.rating:
            self.rating[team] = prior
            self.start[team] = prior
            self.pace[team] = []

    def feed(self, matches: list[dict], collect_fit: bool) -> None:
        for m in matches:
            h, a = m["home"], m["away"]
            prior = m.get("prior", 1500)
            self._ensure(h, prior)
            self._ensure(a, prior)
            gh, ga = m["score"]
            exp = elo_expect(self.rating[h], self.rating[a])
            if collect_fit:
                self._diff_samples.append(
                    (self.rating[h] + ELO_HOME_ADV - self.rating[a], gh - ga)
                )
                self.totals.append(gh + ga)
                self.pace[h].append(gh + ga)
                self.pace[a].append(gh + ga)
            result = 1.0 if gh > ga else (0.0 if gh < ga else 0.5)
            mult = math.log1p(abs(gh - ga))
            delta = ELO_K * mult * (result - exp)
            self.rating[h] += delta
            self.rating[a] -= delta

    def fit(self) -> None:
        # beta: nejmenší čtverce goal_diff ~ elo_diff (bez interceptu krom HA)
        sx = sum(d * d for d, _ in self._diff_samples)
        sxy = sum(d * g for d, g in self._diff_samples)
        if sx > 0:
            self.beta = max(1 / 400, min(1 / 60, sxy / sx))
        homes = [g for d, g in self._diff_samples]
        self.home_goal_edge = (
            sum(homes) / len(homes)
            - (sum(d for d, _ in self._diff_samples) / len(self._diff_samples))
            * self.beta
            if homes
            else 0.3
        )

    def regress(self) -> None:
        for t in self.rating:
            self.rating[t] += REGRESS * (self.start[t] - self.rating[t])

    def team_pace(self, team: str) -> float:
        league = sum(self.totals) / len(self.totals)
        games = self.pace.get(team, [])
        if not games:
            return league
        w = len(games) / (len(games) + 10)
        return w * (sum(games) / len(games)) + (1 - w) * league

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        rh = self.rating.get(home, NEWCOMER_PRIOR)
        ra = self.rating.get(away, NEWCOMER_PRIOR)
        diff = (rh + ELO_HOME_ADV - ra) * self.beta + self.home_goal_edge
        total = 0.5 * (self.team_pace(home) + self.team_pace(away))
        lam_h = max(0.4, (total + diff) / 2)
        lam_a = max(0.4, (total - diff) / 2)
        return lam_h, lam_a

    def probabilities(self, home: str, away: str) -> dict:
        """Pravděpodobnosti výsledku základní hrací doby (1/0/2)."""
        lam_h, lam_a = self.expected_goals(home, away)
        ph = [math.exp(-lam_h) * lam_h**k / math.factorial(k) for k in range(GRID)]
        pa = [math.exp(-lam_a) * lam_a**k / math.factorial(k) for k in range(GRID)]
        p1 = p2 = p0 = 0.0
        for i in range(GRID):
            for j in range(GRID):
                p = ph[i] * pa[j]
                if i > j:
                    p1 += p
                elif i < j:
                    p2 += p
                else:
                    p0 += p
        return {
            "p1": p1,
            "p0": p0,
            "p2": p2,
            "lambda": [round(lam_h, 2), round(lam_a, 2)],
        }


def to_odd(p: float) -> float:
    return round(max(MIN_ODD, min(MAX_ODD, 1.0 / (max(p, 1e-6) * (1 + MARGIN)))), 2)


def market_odds(probs: dict, our_side: str | None = None) -> dict:
    """Klasické trhy na základní hrací dobu: 1, 2, 10 (neprohra domácích), 02.

    Na zápas Bohemians (`our_side` = 'home'/'away') se vypisuje JEDINÝ trh:
    výhra Bohemky. Buď na ni věříš, nebo na ten zápas nesázíš vůbec.
    """
    if our_side == "home":
        return {"1": to_odd(probs["p1"])}
    if our_side == "away":
        return {"2": to_odd(probs["p2"])}
    return {
        "1": to_odd(probs["p1"]),
        "2": to_odd(probs["p2"]),
        "10": to_odd(probs["p1"] + probs["p0"]),
        "02": to_odd(probs["p0"] + probs["p2"]),
    }


def build_model() -> Model:
    model = Model()
    history = load_history()
    model.feed(history, collect_fit=True)
    model.fit()
    model.regress()
    for team, delta in EXPERT_ADJUST.items():
        model._ensure(team, NEWCOMER_PRIOR)
        model.rating[team] += delta
        model.start[team] += delta
    season = json.load(open(DATA / "season.json", encoding="utf-8"))
    played = [m for m in season["matches"] if m["score"]]
    played.sort(key=lambda m: (m["date"] or "9999", m["id"]))
    for m in played:
        # tým bez loňské historie = nováček z kraje, ne průměrný divizní tým
        m["prior"] = NEWCOMER_PRIOR
    model.feed(played, collect_fit=False)
    return model


def current_round(matches: list[dict], published: dict) -> int | None:
    """Kolo s datumově nejbližším neodehraným a nevypsaným zápasem."""
    candidates = [
        m
        for m in matches
        if not m["score"] and str(m["id"]) not in published and m["round"]
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda m: (m["date"] or "9999", m["time"] or "99"))[
        "round"
    ]


def main() -> None:
    refresh = "--refresh" in sys.argv
    model = build_model()
    season = json.load(open(DATA / "season.json", encoding="utf-8"))
    pub_path = DATA / "published.json"
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}

    if refresh:
        targets = [
            m for m in season["matches"] if str(m["id"]) in published and not m["score"]
        ]
        verb = "přepočteno"
    else:
        # nové kolo se vypisuje až po dohrání toho vypsaného (dohrávku obejde --force)
        open_matches = [
            m for m in season["matches"] if str(m["id"]) in published and not m["score"]
        ]
        if open_matches and "--force" not in sys.argv:
            print("Vypsané kolo ještě není dohrané, nové se nevypisuje. Čeká se na:")
            for m in open_matches:
                print(f"  {m['round']}. kolo: {m['home']} - {m['away']} ({m['date']})")
            print("(dohrávku lze přeskočit pomocí --force)")
            return
        rnd = current_round(season["matches"], published)
        if rnd is None:
            print("Není co vypsat — všechny neodehrané zápasy už mají kurz.")
            return
        targets = [
            m
            for m in season["matches"]
            if m["round"] == rnd and not m["score"] and str(m["id"]) not in published
        ]
        verb = f"vypsáno {rnd}. kolo"

    for m in targets:
        probs = model.probabilities(m["home"], m["away"])
        our_side = (
            "home"
            if m["home"] == OUR_TEAM
            else ("away" if m["away"] == OUR_TEAM else None)
        )
        published[str(m["id"])] = {
            "match_id": m["id"],
            "round": m["round"],
            "home": m["home"],
            "away": m["away"],
            "special": our_side is not None,
            "odds": market_odds(probs, our_side=our_side),
            "model": {
                "p1": round(probs["p1"], 4),
                "p0": round(probs["p0"], 4),
                "p2": round(probs["p2"], 4),
                "lambda": probs["lambda"],
                "elo": [
                    round(model.rating.get(m["home"], NEWCOMER_PRIOR)),
                    round(model.rating.get(m["away"], NEWCOMER_PRIOR)),
                ],
            },
            "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    pub_path.write_text(json.dumps(published, ensure_ascii=False, indent=1))
    print(f"Kurzy: {verb}, {len(targets)} zápasů -> data/published.json")
    for m in targets:
        o = published[str(m["id"])]["odds"]
        star = " ⭐ jen výhra Bohemky" if published[str(m["id"])]["special"] else ""
        cols = "  ".join(
            f"{mk}:{o[mk]:5.2f}" for mk in ("1", "10", "02", "2") if mk in o
        )
        print(f"  {m['home']:26s} - {m['away']:26s}  {cols}{star}")


if __name__ == "__main__":
    main()
