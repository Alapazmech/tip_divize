"""Zkušební liga — dočasná záložka na osahání celého sázkového cyklu.

Fixture je 1. a 2. kolo Divize A (skutečný los, ale výsledky se LOSUJÍ).
Sázení běží přes bota jako v ostré hře, jen nad oddělenými demo_* soubory
(zapíná je vlajka data/demo_active, viz tickets.is_demo()).

    python3 demo.py start   # stáhne fixture, vypíše 1. kolo, zapne režim
    python3 demo.py step    # = bot /update: vylosuje výsledky, vyhodnotí,
                            #   vypíše další kolo (nebo ohlásí konec)
    python3 demo.py stop    # smaže demo soubory a vypne režim

Ostrá data (season.json, published.json, bets.csv…) zůstávají nedotčená.
"""

import json
import math
import pathlib
import random
import subprocess
import sys
import time

import generate_site as gs
import odds
import scraper
import tickets

DATA = pathlib.Path(__file__).parent / "data"
SEASON = DATA / "demo_season.json"
PUBLISHED = DATA / "demo_published.json"
FILES = [
    SEASON,
    PUBLISHED,
    DATA / "demo_bets.csv",
    DATA / "demo_bets_sealed.json",
    DATA / "demo_commitments.json",
]

DEMO_SOURCE = {
    "code": "8XM4",
    "alias": "8XM4-A",
    "fis_id": 4822,
    "label": "Zkušební liga (los Divize A)",
    "start_year": 2026,
}
DEMO_ROUNDS = (1, 2)


def _load_pub() -> dict:
    return json.loads(PUBLISHED.read_text()) if PUBLISHED.exists() else {}


def _publish_next(model) -> list[dict]:
    """Vypíše kurzy dalšího zkušebního kola (obdoba odds.main pro demo)."""
    season = json.load(open(SEASON, encoding="utf-8"))
    pub = _load_pub()
    rnd = odds.current_round(season["matches"], pub)
    if rnd is None:
        return []
    targets = [
        m
        for m in season["matches"]
        if m["round"] == rnd and not m["score"] and str(m["id"]) not in pub
    ]
    for m in targets:
        probs = model.probabilities(m["home"], m["away"])
        pub[str(m["id"])] = {
            "match_id": m["id"],
            "round": m["round"],
            "home": m["home"],
            "away": m["away"],
            "special": False,
            "odds": odds.market_odds(probs),
            "model": {"lambda": probs["lambda"]},
            "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    PUBLISHED.write_text(json.dumps(pub, ensure_ascii=False, indent=1))
    return targets


def _poisson(lam: float) -> int:
    limit = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= random.random()
        if p <= limit:
            return k
        k += 1


def _randomize(model, m: dict) -> None:
    lam_h, lam_a = model.expected_goals(m["home"], m["away"])
    gh, ga = _poisson(lam_h), _poisson(lam_a)
    m["overtime"] = m["shootout"] = False
    if gh == ga:
        home_wins = random.random() < odds.elo_expect(
            model.rating.get(m["home"], odds.NEWCOMER_PRIOR),
            model.rating.get(m["away"], odds.NEWCOMER_PRIOR),
        )
        gh, ga = (gh + 1, ga) if home_wins else (gh, ga + 1)
        m["overtime"] = True
        m["shootout"] = random.random() < 0.5
    m["score"] = [gh, ga]
    m["status"] = "odehráno (vylosováno)"


def _score_note(m: dict) -> str:
    return " sn" if m.get("shootout") else (" p" if m.get("overtime") else "")


def _round_odds_text(targets: list[dict]) -> str:
    pub = _load_pub()
    lines = [f"🏑 Vypsané {targets[0]['round']}. zkušební kolo:"]
    for m in targets:
        o = pub[str(m["id"])]["odds"]
        cols = "  ".join(
            f"{mk}: {o[mk]:.2f}" for mk in ("1", "10", "02", "2") if mk in o
        )
        lines.append(f"{m['home']} – {m['away']}\n   {cols}")
    return "\n".join(lines)


def _settle_summary(rnd: int) -> str:
    """Jen řádky vyhodnocení — výsledky a kurzy jsou na stránce."""
    season = json.load(open(SEASON, encoding="utf-8"))
    state = gs.settle(season["matches"], _load_pub(), DATA / "demo_bets.csv")
    if not state["banks"]:
        return f"📊 {rnd}. zkušební kolo dohráno — nikdo nesázel."
    lines = [f"📊 Vyhodnocení {rnd}. zkušebního kola:"]
    per = {p: 0.0 for p in state["banks"]}
    for t in state["settled"].get(rnd, []):
        per[t["person"]] = per.get(t["person"], 0.0) + t["delta"]
    for p, d in sorted(per.items(), key=lambda x: (-x[1], x[0])):
        mark = "✅" if d > 0 else ("❌" if d < 0 else "➖")
        lines.append(f"{mark} {p}: {d:+.0f}  (bank {state['banks'][p]:.0f})")
    return "\n".join(lines)


def _push(msg: str) -> None:
    root = pathlib.Path(__file__).parent
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode:
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=root)
    hook = DATA / "render_hook.txt"
    if hook.exists():  # Render nemá webhook na repu — deploy budí hook
        import urllib.request

        try:
            urllib.request.urlopen(hook.read_text().strip(), timeout=15).read()
        except Exception as exc:
            print("deploy hook selhal:", exc)


def start() -> str:
    data = scraper.scrape(DEMO_SOURCE)
    data["matches"] = [m for m in data["matches"] if m["round"] in DEMO_ROUNDS]
    for m in data["matches"]:
        m["score"] = None  # kdyby už se v realitě hrálo, testu je to jedno
        m.pop("score_note", None)
    SEASON.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (DATA / "demo_bets.csv").write_text("round,person,ticket,match,market,stake\n")
    tickets.DEMO_FLAG.touch()
    targets = _publish_next(odds.build_model())
    gs.main()
    _push("Zkušební liga: start")
    del targets
    return (
        "🧪 Zkušební liga odstartována — kurzy najdete v záložce „Test“ na stránce. "
        "Sázejte jako v ostré hře; výsledky se pak vylosují."
    )


def step() -> str:
    if not SEASON.exists():
        return "Zkušební liga neběží — spusť ji přes /demo start."
    season = json.load(open(SEASON, encoding="utf-8"))
    pub = _load_pub()
    model = odds.build_model()
    open_matches = [
        m for m in season["matches"] if str(m["id"]) in pub and not m["score"]
    ]
    parts = []
    if open_matches:
        for m in open_matches:
            _randomize(model, m)
        SEASON.write_text(json.dumps(season, ensure_ascii=False, indent=1))
        tickets.reveal_completed()
        parts.append(_settle_summary(open_matches[0]["round"]))
    targets = _publish_next(model)
    gs.main()
    _push("Zkušební liga: další kolo")
    if not targets:
        parts.append(
            "🏁 Zkušební liga dohrána! Ukončete ji příkazem /demo stop "
            "(záložka zmizí a ostrá hra pojede načisto)."
        )
    return "\n\n".join(parts) if parts else "Není co vyhodnocovat."


def stop() -> str:
    tickets.DEMO_FLAG.unlink(missing_ok=True)
    for f in FILES:
        f.unlink(missing_ok=True)
    gs.main()
    _push("Zkušební liga: konec, úklid")
    return "🧪 Zkušební liga ukončena a uklizena. Ostrá hra jede dál načisto."


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "step"
    print({"start": start, "step": step, "stop": stop}[mode]())
