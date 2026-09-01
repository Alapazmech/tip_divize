"""Tajné tikety — commit–reveal.

Sázkař pošle tiket botovi do SOUKROMÉ zprávy (identita = Telegram účet,
žádná hesla). Tiket se uloží do data/bets_sealed.json (gitignored, vidí ho
jen bookmaker) a veřejně se publikuje jen jeho SHA-256 otisk
v data/commitments.json — na stránce visí „🔒 Kunc — tiket #a3f2c1".

Po dohrání kola `reveal_completed()` (volá ho update.sh) tikety odhalí:
zapíše je do bets.csv (vypořádání beze změny) a do commitments doplní
obsah + nonce, takže si každý může hash přepočítat a ověřit, že se tiket
po vsazení neměnil ani nepřidával zpětně.
"""

import base64
import datetime
import hashlib
import json
import pathlib
import secrets

import generate_site as gs

DATA = pathlib.Path(__file__).parent / "data"
PLAYERS = DATA / "players.json"  # telegram user_id -> jméno sázkaře (společné)
DEMO_FLAG = DATA / "demo_active"


def is_demo() -> bool:
    """Zkušební režim: všechno sázení běží nad demo_* soubory."""
    return DEMO_FLAG.exists()


def _p(name: str) -> pathlib.Path:
    """Datový soubor podle režimu: bets.csv vs demo_bets.csv apod."""
    return DATA / (("demo_" if is_demo() else "") + name)


def _load(path: pathlib.Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save(path: pathlib.Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def _season():
    return json.load(open(_p("season.json"), encoding="utf-8"))


def _published():
    return _load(_p("published.json"), {})


def person_for(user_id: int, fallback: str) -> str:
    players = _load(PLAYERS, {})
    key = str(user_id)
    if key not in players:
        players[key] = fallback
        _save(PLAYERS, players)
    return players[key]


def open_round(season: dict, published: dict) -> tuple[int | None, list[dict]]:
    """Vypsané kolo (kurzy venku, ještě se hraje) a jeho zápasy."""
    by_id = {m["id"]: m for m in season["matches"]}
    open_matches = [
        by_id[v["match_id"]]
        for v in published.values()
        if not by_id[v["match_id"]].get("score")
    ]
    if not open_matches:
        return None, []
    rnd = min(m["round"] for m in open_matches)
    return rnd, [m for m in season["matches"] if m["round"] == rnd]


def deadline(m: dict) -> datetime.datetime:
    """Uzávěrka = začátek zápasu (neznámý čas -> 10:00 v den zápasu)."""
    t = m["time"] if m["time"] and m["time"] != "00:00" else "10:00"
    return datetime.datetime.fromisoformat(f"{m['date']}T{t}")


def available_bank(person: str) -> float:
    """Bank po vypořádání minus vklady ve hře (bets.csv i zapečetěné)."""
    season = _season()
    state = gs.settle(season["matches"], _published(), _p("bets.csv"))
    bank = state["banks"].get(person, gs.START_BANK)
    in_play = sum(
        t["stake"]
        for rows in state["open"].values()
        for t in rows
        if t["person"] == person
    )
    sealed = sum(
        t["stake"] for t in _load(_p("bets_sealed.json"), []) if t["person"] == person
    )
    return bank - in_play - sealed


def _ticket_hash(ticket: dict) -> str:
    canonical = json.dumps(
        {
            "round": ticket["round"],
            "person": ticket["person"],
            "stake": ticket["stake"],
            "legs": [[leg["match_id"], leg["market"]] for leg in ticket["legs"]],
            "nonce": ticket["nonce"],
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def place(
    user_id: int,
    person: str,
    stake: float,
    legs_spec: list[tuple[str, str]],
    code_hash: str | None = None,
) -> tuple[bool, str, str | None]:
    """Přijme tiket (legs_spec = [(tým/id, trh), …]).

    Vrací (ok, odpověď pro sázkaře, hash tiketu).
    """
    season = _season()
    published = _published()
    rnd, matches = open_round(season, published)
    if rnd is None:
        return False, "Teď není vypsané žádné kolo — počkej na nové kurzy.", None

    now = datetime.datetime.now()
    legs = []
    for team_ref, market in legs_spec:
        m = gs.resolve_match(str(team_ref), matches)
        if not m:
            return (
                False,
                f"Nenašel jsem jednoznačný zápas pro „{team_ref}“ v {rnd}. kole.",
                None,
            )
        entry = published.get(str(m["id"]))
        if not entry or market not in entry["odds"]:
            if entry and entry.get("special"):
                return (
                    False,
                    f"Na zápas {m['home']} – {m['away']} jde vsadit jedině výhra "
                    "Bohemky — buď věříš, nebo nesázíš. 🏑",
                    None,
                )
            return (
                False,
                f"Trh „{market}“ není u zápasu {m['home']} – {m['away']} vypsaný.",
                None,
            )
        if m.get("score") or deadline(m) <= now:
            return (
                False,
                f"Zápas {m['home']} – {m['away']} už začal/skončil — pozdě.",
                None,
            )
        if any(leg["match_id"] == m["id"] for leg in legs):
            return False, "Stejný zápas nemůže být na tiketu dvakrát.", None
        legs.append(
            {"match_id": m["id"], "market": market, "odd": entry["odds"][market]}
        )

    if stake <= 0:
        return False, "Vklad musí být kladný.", None
    avail = available_bank(person)
    if stake > avail:
        return False, f"Na to nemáš — k dispozici máš {avail:.0f}.", None

    total_odd = 1.0
    for leg in legs:
        total_odd *= leg["odd"]
    ticket = {
        "round": rnd,
        "user_id": user_id,
        "person": person,
        "stake": stake,
        "legs": legs,
        "nonce": secrets.token_hex(8),
        "placed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "code_hash": code_hash,
    }
    h = _ticket_hash(ticket)
    ticket["hash"] = h

    sealed = _load(_p("bets_sealed.json"), [])
    sealed.append(ticket)
    _save(_p("bets_sealed.json"), sealed)
    commits = _load(_p("commitments.json"), [])
    commits.append(
        {"hash": h, "person": person, "round": rnd, "placed_at": ticket["placed_at"]}
    )
    _save(_p("commitments.json"), commits)

    by_id = {m["id"]: m for m in season["matches"]}
    lines = [f"🔒 Tiket přijat (#{h[:8]}), {rnd}. kolo:"]
    for leg in legs:
        m = by_id[leg["match_id"]]
        lines.append(f"  {m['home']} – {m['away']}  {leg['market']} @ {leg['odd']:.2f}")
    lines.append(
        f"Vklad {stake:.0f}, celkový kurz {total_odd:.2f}, možná výhra "
        f"{stake * total_odd:.0f}. Zbývá ti {avail - stake:.0f}."
    )
    return True, "\n".join(lines), h


def decrypt_tip(code: str) -> dict:
    """Rozbalí kód „tip: …“ ze stránky: base64(ephemeral_pk ‖ nonce ‖ box)."""
    from nacl.public import Box, PrivateKey, PublicKey

    raw = base64.b64decode(code)
    if len(raw) < 32 + 24 + 1:
        raise ValueError("kód je moc krátký")
    secret = bytes.fromhex((DATA / "secret_key.txt").read_text().strip())
    box = Box(PrivateKey(secret), PublicKey(raw[:32]))
    return json.loads(box.decrypt(raw[56:], raw[32:56]))


def place_from_tip(
    user_id: int, person: str, code: str
) -> tuple[bool, str, str | None]:
    """Dešifruje kód ze stránky a vsadí tiket.

    Stejný kód podruhé = omylem přeposlaný tiket, ne nová sázka.
    """
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    for t in _load(_p("bets_sealed.json"), []):
        if t.get("code_hash") == code_hash:
            return (
                False,
                f"Tenhle kód už mám zapečetěný (#{t['hash'][:8]}) — tiket platí "
                "jen jednou. Chceš-li stejnou sázku znovu, naklikej nový tiket.",
                None,
            )
    try:
        payload = decrypt_tip(code)
        stake = float(payload["stake"])
        legs_spec = [(str(mid), str(mk)) for mid, mk in payload["legs"]]
    except Exception:
        return (
            False,
            "Kód tiketu se nepodařilo rozbalit — zkopíroval jsi ho celý?",
            None,
        )
    return place(user_id, person, stake, legs_spec, code_hash=code_hash)


def storno(user_id: int) -> str:
    """Zruší uživatelovy zapečetěné tikety, u kterých ještě nic nezačalo."""
    season = _season()
    by_id = {m["id"]: m for m in season["matches"]}
    now = datetime.datetime.now()
    sealed = _load(_p("bets_sealed.json"), [])
    keep, cancelled = [], []
    for t in sealed:
        mine = t["user_id"] == user_id
        started = any(deadline(by_id[leg["match_id"]]) <= now for leg in t["legs"])
        (cancelled if mine and not started else keep).append(t)
    if not cancelled:
        return "Nemáš žádný tiket, který by šel stornovat."
    _save(_p("bets_sealed.json"), keep)
    gone = {t["hash"] for t in cancelled}
    _save(
        _p("commitments.json"),
        [c for c in _load(_p("commitments.json"), []) if c["hash"] not in gone],
    )
    return f"Stornováno tiketů: {len(cancelled)}. Vklady se vrací do banku."


def my_tickets(user_id: int, person: str) -> str:
    season = _season()
    by_id = {m["id"]: m for m in season["matches"]}
    mine = [t for t in _load(_p("bets_sealed.json"), []) if t["user_id"] == user_id]
    lines = [f"Bank k dispozici: {available_bank(person):.0f}"]
    if not mine:
        lines.append("Žádný zapečetěný tiket.")
    for t in mine:
        legs = ", ".join(
            f"{by_id[leg['match_id']]['home_short'] or by_id[leg['match_id']]['home']}"
            f" {leg['market']} @{leg['odd']:.2f}"
            for leg in t["legs"]
        )
        lines.append(
            f"🔒 #{t['hash'][:8]} ({t['round']}. kolo): {legs} — vklad {t['stake']:.0f}"
        )
    return "\n".join(lines)


def reveal_completed() -> list[str]:
    """Odhalí tikety dohraných kol -> bets.csv + doplní commitments."""
    season = _season()
    by_round: dict[int, list[dict]] = {}
    for m in season["matches"]:
        if m["round"]:
            by_round.setdefault(m["round"], []).append(m)
    sealed = _load(_p("bets_sealed.json"), [])
    commits = _load(_p("commitments.json"), [])
    by_hash = {c["hash"]: c for c in commits}

    keep, revealed_msgs = [], []
    bets_path = _p("bets.csv")
    if not bets_path.exists():
        bets_path.write_text("round,person,ticket,match,market,stake\n")
    lines_to_append = []
    for t in sealed:
        done = all(m["score"] for m in by_round.get(t["round"], []))
        if not done:
            keep.append(t)
            continue
        for leg in t["legs"]:
            lines_to_append.append(
                f"{t['round']},{t['person']},{t['hash'][:8]},{leg['match_id']},{leg['market']},{t['stake']:.0f}"
            )
        c = by_hash.get(t["hash"])
        if c is not None:
            c["revealed"] = {
                "stake": t["stake"],
                "legs": [[leg["match_id"], leg["market"]] for leg in t["legs"]],
                "nonce": t["nonce"],
                "placed_at": t["placed_at"],
            }
        revealed_msgs.append(f"{t['person']} #{t['hash'][:8]} ({t['round']}. kolo)")
    if lines_to_append:
        with open(bets_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines_to_append) + "\n")
        _save(_p("bets_sealed.json"), keep)
        _save(_p("commitments.json"), commits)
    return revealed_msgs


if __name__ == "__main__":
    msgs = reveal_completed()
    if msgs:
        print("Odhalené tikety:", ", ".join(msgs))
    else:
        print("Žádné tikety k odhalení.")
