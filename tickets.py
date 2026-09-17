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


def open_matches(season: dict, published: dict) -> list[dict]:
    """Všechny vypsané a neodehrané zápasy (může jít o víc kol — dohrávky)."""
    by_id = {m["id"]: m for m in season["matches"]}
    return [
        by_id[v["match_id"]]
        for v in published.values()
        if not by_id[v["match_id"]].get("score")
    ]


def deadline(m: dict) -> datetime.datetime:
    """Uzávěrka = začátek zápasu (neznámý čas -> 10:00 v den zápasu)."""
    t = m["time"] if m["time"] and m["time"] != "00:00" else "10:00"
    return datetime.datetime.fromisoformat(f"{m['date']}T{t}")


def available_bank(person: str) -> float:
    """Bank po vypořádání minus vklady živých tiketů (bets.csv i bets_sealed)."""
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


def dokoupit(user_id: int, person: str) -> str:
    """Dokup po prohře všeho: zaplatí BUYIN_KC, dostane START_BANK kreditů.
    Kdykoliv a kolikrát chce.

    Projde jen když je bank na nule a sázkař nemá živý (= podaný, ještě
    nevyhodnocený) tiket. Zapíše řádek do topups.csv; kredity platí od
    aktuálního (nebo příštího) kola. Peníze se řeší až na konci základní části.
    """
    season = _season()
    published = _published()
    state = gs.settle(season["matches"], published, _p("bets.csv"))
    if person not in state["banks"]:
        return f"Bank není 0, je {gs.START_BANK}."
    bank = state["banks"][person]
    # živý tiket = podaný a ještě nevyhodnocený (bets_sealed.json = podané tikety
    # čekající na dohrání kola; bets.csv "open" = odhalené, čekající na dohrávku)
    live = sum(
        1 for rows in state["open"].values() for t in rows if t["person"] == person
    ) + sum(1 for t in _load(_p("bets_sealed.json"), []) if t["user_id"] == user_id)
    if bank >= 1:
        return f"Bank není 0, je {bank:.0f}."
    if live:
        return "Máš ještě živý tiket."

    done = state["deposits"][person]["topups"]
    credits = gs.START_BANK
    # kredity platí od kola, na které se právě sází; když žádné otevřené není,
    # od příštího vypsaného
    open_rounds = [
        v["round"] for m in open_matches(season, published)
        for v in [published[str(m["id"])]]
    ]
    latest = max((v["round"] for v in published.values()), default=0)
    rnd = max(open_rounds) if open_rounds else latest + 1

    path = _p("topups.csv")
    if not path.exists():
        path.write_text("round,person,credits,paid\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{rnd},{person},{credits},{gs.BUYIN_KC}\n")
    return (
        f"✅ V pořádku, {done + 1}. dokup: máš {credits} kreditů (od {rnd}. kola). "
        f"Celkem vloženo {state['deposits'][person]['kc'] + gs.BUYIN_KC:.0f} Kč."
    )


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
    all_open = open_matches(season, published)
    if not all_open:
        return False, "Teď není vypsané žádné kolo — počkej na nové kurzy.", None
    # sází se aktuální (nejnovější vypsané) kolo; odložená dohrávka ze
    # staršího kola jde s ním, pokud se hraje dřív než začne příští kolo
    latest = max(v["round"] for v in published.values())
    next_ms = [m for m in season["matches"] if m["round"] == latest + 1]

    now = datetime.datetime.now()
    legs = []
    for team_ref, market in legs_spec:
        m = gs.resolve_match(str(team_ref), all_open)
        if not m:
            return (
                False,
                f"Nenašel jsem jednoznačný zápas pro „{team_ref}“ mezi vypsanými.",
                None,
            )
        if m["round"] != latest and not gs.dohravka_bettable(m, next_ms):
            return (
                False,
                f"Dohrávka {m['home']} – {m['away']} se hraje až v dalším kole — "
                "sázky se otevřou s ním.",
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

    rnd = latest
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
    lines = [f"🔒 Tiket podán (#{h[:8]}), {rnd}. kolo:"]
    for leg in legs:
        m = by_id[leg["match_id"]]
        lines.append(f"  {m['home']} – {m['away']}  {leg['market']} @ {leg['odd']:.2f}")
    lines.append(
        f"Vklad {stake:.0f}, celkový kurz {total_odd:.2f}, možná výhra "
        f"{stake * total_odd:.0f}. Zbývá ti {avail - stake:.0f}."
    )
    return True, "\n".join(lines), h


MK_CODE = {"1": 0, "2": 1, "10": 2, "02": 3, "0": 4}
MK_FROM_CODE = {v: k for k, v in MK_CODE.items()}


def decrypt_tip(code: str) -> dict:
    """Rozbalí kód „tip: …“ ze stránky.

    v2 (krátký): base64(ephemeral_pk ‖ box), nonce = SHA-512(epk‖pk)[:24],
    payload binárně: vklad uint24 ‖ počet legů ‖ (match_id uint32 ‖ trh u8)*.
    v1 (starý, fallback): base64(epk ‖ nonce ‖ box) s JSON payloadem.
    """
    from nacl.public import Box, PrivateKey, PublicKey

    raw = base64.b64decode(code)
    if len(raw) < 32 + 17:
        raise ValueError("kód je moc krátký")
    sk = PrivateKey(bytes.fromhex((DATA / "secret_key.txt").read_text().strip()))
    pk = bytes(sk.public_key)
    epk = raw[:32]
    try:
        nonce = hashlib.sha512(epk + pk).digest()[:24]
        body = Box(sk, PublicKey(epk)).decrypt(raw[32:], nonce)
        stake = int.from_bytes(body[0:3], "big")
        legs = [
            [
                int.from_bytes(body[4 + i * 5 : 8 + i * 5], "big"),
                MK_FROM_CODE[body[8 + i * 5]],
            ]
            for i in range(body[3])
        ]
        return {"stake": stake, "legs": legs}
    except Exception:
        box = Box(sk, PublicKey(epk))
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
                f"Tenhle tiket už je podaný (#{t['hash'][:8]}) — jeden kód platí "
                "jen jednou. Chceš-li stejnou sázku znovu, naklikej nový tiket.",
                None,
            )
    try:
        payload = decrypt_tip(code)
        stake = float(payload["stake"])
        legs_spec = [(str(mid), str(mk)) for mid, mk in payload["legs"]]
    except Exception as exc:
        # celý kód do logu, ať jde selhání zpětně přehrát (log vidí jen bookmaker)
        print(
            f"[tip] rozbalení selhalo ({type(exc).__name__}: {exc}), "
            f"{len(code)} znaků: {code}",
            flush=True,
        )
        return (
            False,
            f"Kód tiketu se nepodařilo rozbalit (dorazilo {len(code)} znaků) — "
            "zkopíruj ho celý tlačítkem „Zkopírovat“ a pošli znovu.",
            None,
        )
    return place(user_id, person, stake, legs_spec, code_hash=code_hash)


def storno(user_id: int) -> str:
    """Zruší uživatelovy živé tikety, u kterých ještě nic nezačalo."""
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
    published = _published()
    by_id = {m["id"]: m for m in season["matches"]}
    latest = max((v["round"] for v in published.values()), default=None)
    mine = [t for t in _load(_p("bets_sealed.json"), []) if t["user_id"] == user_id]
    lines = [f"Bank k dispozici: {available_bank(person):.0f}"]
    if not mine:
        lines.append("Žádný živý tiket.")
    for t in mine:
        legs = ", ".join(
            f"{by_id[leg['match_id']]['home_short'] or by_id[leg['match_id']]['home']}"
            f" {leg['market']} @{leg['odd']:.2f}"
            for leg in t["legs"]
        )
        waiting = " ⏳ čeká na dohrávku" if latest and t["round"] < latest else ""
        lines.append(
            f"🔒 #{t['hash'][:8]} ({t['round']}. kolo): {legs} — vklad {t['stake']:.0f}{waiting}"
        )
    return "\n".join(lines)


def reveal_completed() -> list[str]:
    """Odhalí tikety, jejichž VŠECHNY zápasy jsou dohrané -> bets.csv.

    Tiket s odloženým zápasem (dohrávkou) zůstává zapečetěný a čeká,
    dokud se nedohraje i on; ostatní tikety kola se odhalí normálně.
    """
    season = _season()
    by_id = {m["id"]: m for m in season["matches"]}
    sealed = _load(_p("bets_sealed.json"), [])
    commits = _load(_p("commitments.json"), [])
    by_hash = {c["hash"]: c for c in commits}

    keep, revealed_msgs = [], []
    bets_path = _p("bets.csv")
    if not bets_path.exists():
        bets_path.write_text("round,person,ticket,match,market,stake\n")
    lines_to_append = []
    for t in sealed:
        done = all(by_id[leg["match_id"]].get("score") for leg in t["legs"])
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
