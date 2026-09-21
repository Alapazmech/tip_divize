"""Telegram bot Tipdivize — člen sázkového chatu.

Poslouchá skupinu přes oficiální Bot API (long polling, čisté stdlib) a umí:
  „tip: …" kód ze stránky   — přijme tajný tiket; platný dostane jen ✅
                               reakci (žádný spam), chybný krátkou odpověď
  „updatuj kurzy" / /update  — spustí ./update.sh a pošle, co se stalo
                               (jen pro adminy z configu)
  /banky                     — stav bank sázkařů
  /vysledky                  — vyhodnocení dohraných kol (kdo skončil na
                               nule, dostane hlášku s pobídkou k dokupu)
  /dokoupit                  — po prohře všeho: bot ověří nulový bank a žádný
                               živý (podaný, nevyhodnocený) tiket,
                               zapíše dokup (100 kreditů za 100 Kč, bez limitu)
Nic jiného bot neumí a jiné zprávy mlčky ignoruje.

Sám hlídá dohrané zápasy: od 2 h po začátku každého vypsaného zápasu bez
výsledku spouští každou půlhodinu update.sh (max. 10 h po začátku, pak to
nechá na denním timeru) a do skupiny pošle nově vyhodnocené tikety —
klidně jen sobotní část kola, nedělní přijde zvlášť. Co už hlásil, si
pamatuje v data/reported.json; při prvním startu si tam zapíše všechno
dosud vyhodnocené, aby nespamoval historii. Hráče oslovuje 5. pádem
(cestina.py).

Nastavení (jednorázově):
  1. U @BotFather: /newbot -> token; /setprivacy -> Disable (jinak bot ve
     skupině nevidí obyčejné zprávy, jen /příkazy).
  2. Přidat bota do skupiny.
  3. Vytvořit data/telegram.json (je v .gitignore!):
     {"token": "123:ABC", "chat_id": null, "admins": ["mschejbal"]}
     chat_id null = bot reaguje všude; id skupiny je v logu bota
     ([msg] chat=…) — doplň ho, ať reaguje jen ve vašem chatu.
  4. Spustit: python3 telegram_bot.py  (např. v tmux / systemd)

Spuštění update je frontované — bot zpracovává zprávy sériově, takže dvě
rychlá „updatuj" za sebou nespustí dva scrapy najednou.
"""

import datetime
import json
import pathlib
import re
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request

import tickets
from cestina import genitiv, vokativ

TIP_RE = re.compile(r"tip:\s*([A-Za-z0-9+/=]{40,})")

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
CONFIG = DATA / "telegram.json"
OFFSET = DATA / "telegram_offset.txt"

# automatické uzavírání: kdy po začátku zápasu začít zjišťovat výsledek,
# jak dlouho to zkoušet a jak často
CHECK_AFTER = 2 * 3600
CHECK_WINDOW = 10 * 3600
CHECK_EVERY = 30 * 60
REPORT_EVERY = 10 * 60

OUR_TEAM = "FbŠ Florbal Bohemians"


def api(token: str, method: str, **params) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=70) as resp:
        return json.loads(resp.read())


def send(token: str, chat_id: int, text: str, reply_to: int | None = None) -> None:
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    try:
        api(token, "sendMessage", **params)
    except Exception as exc:
        print("sendMessage selhal:", exc)


def react(token: str, chat_id: int, message_id: int) -> None:
    """Fajfka na zprávu s tiketem. Telegram povoluje jen pevnou sadu
    reakčních emoji — když ✅ neprojde, zkusí se 👍."""
    for emoji in ("✅", "👍"):
        try:
            api(
                token,
                "setMessageReaction",
                chat_id=chat_id,
                message_id=message_id,
                reaction=json.dumps([{"type": "emoji", "emoji": emoji}]),
            )
            return
        except Exception:
            continue
    print(f"reakce na zprávu {message_id} neprošla")


def banks_summary() -> str:
    """Jen jména a banky. Kdo ještě nesázel, má startovní bank."""
    import generate_site

    season = json.load(open(tickets._p("season.json"), encoding="utf-8"))
    pub_path = tickets._p("published.json")
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    state = generate_site.settle(season["matches"], published, tickets._p("bets.csv"))
    banks = dict(state["banks"])
    for name in tickets._load(tickets.PLAYERS, {}).values():
        banks.setdefault(name, float(generate_site.START_BANK))
    lines = ["💰 Banky:"]
    key = lambda x: (-x[1], unicodedata.normalize("NFKD", x[0]).encode("ascii", "ignore").lower())
    for i, (p, b) in enumerate(sorted(banks.items(), key=key), 1):
        lines.append(f"{i}. {p}: {b:.0f}")
    return "\n".join(lines)


# {v} = oslovení (5. pád), {p} = jméno (1. pád), {g} = 2. pád („bank hráče …“)
BROKE_LINES = (
    "Tyjo, to byla fakt smůla, {v}. {team} je kousavá potvůrka. 🐾 Co takhle si dokoupit další dukáty? /dokoupit",
    "{v}, tým {team} ti sebral poslední dukáty. 💸 Nevadí, mincovna má otevřeno: /dokoupit",
    "Au, {v}. Tým {team} ti vybral bank do posledního dukátu. 🪙 Za stovku nová truhla: /dokoupit",
    "{v}, tohle bolelo. Tým {team} zařídil nulu na kontě. Dukáty se dají dokoupit, hrdost ne. 🛡️ /dokoupit",
    "{p} je na nule a může za to {team}. 🏑 Doplň dukáty a vrať jim to: /dokoupit",
    "Smůla, {v}. Tým {team} dneska kousal. Truhla je prázdná, ale /dokoupit ji naplní. 🪙",
    "{v}, tým {team} ti sfoukl poslední dukát. 🕯️ Nová stovka, nový bank: /dokoupit",
    "{p} je bez dukátů, tým {team} byl bez slitování. 🧾 /dokoupit a jde se znovu.",
    "{v}, bank 0. Tým {team} se prostě nezeptal. 🤷 Dukáty na dokoupení jsou za stovku: /dokoupit",
    "Kdo by to od týmu {team} čekal, že, {v}? 😅 Poslední dukát je pryč, ale /dokoupit tě vrátí do hry.",
    "{v}, tým {team} ti právě ukázal, proč se říká „florbal je nevyzpytatelný“. 🎲 Stovka na stůl: /dokoupit",
    "Bank hráče {g}: 0. Nálada hráče {g}: taky. 📉 Tým {team} se omlouvá, bookmaker ne. /dokoupit",
    "{v}, tým {team} tě poslal do šatny s prázdnou kapsou. 🧦 Dukáty čekají ve výdejně: /dokoupit",
    "{p} zavírá krám, tým {team} vyprodal zásoby. 🏪 Znovu otevřeno po /dokoupit.",
    "Tým {team} dneska hrál jako o život a {p} to odnesl. 🚑 První pomoc: /dokoupit",
    "{v}, nula je jen začátek každého velkého comebacku. 🔄 Tým {team} tě jen rozehřál. /dokoupit",
    "Gratulace, {v}, máš první čistý bank sezóny. Zásluhu si připisuje tým {team}. 🧼 /dokoupit",
    "{v}, tým {team} si vzal tvé dukáty a nevrátí je. 🏴‍☠️ Ale stovka koupí novou loď: /dokoupit",
    "Někdy vyhraješ, někdy hraje tým {team}. {p} dneska zažil to druhé. 🤕 /dokoupit",
    "{v}, bank na nule, hlava vzhůru. Tým {team} to nemyslel osobně. 🫂 /dokoupit",
    "Tým {team} právě sfoukl bank hráče {g} jako svíčku na dortu. 🎂 Přání: /dokoupit",
    "{v}, výsledek dnes napsal tým {team} a tvůj bank to nepřežil. ✍️ Nová kapitola: /dokoupit",
    "Tabulka banků má novou nulu: {p}. Sponzorem je tým {team}. 🏷️ Odsponzoruj se zpátky: /dokoupit",
    "{v}, tvé dukáty odjely s autobusem týmu {team}. 🚌 Další spoj jede po /dokoupit.",
    "Tým {team} ti dal lekci, {v}. Školné bylo sto korun, opakovačka taky: /dokoupit 🎓",
    "{v}, mezi tebou a bankem 0 už nic nestojí. Postaral se tým {team}. 🧱 /dokoupit a stav znovu.",
    "Ještě že dukáty nejsou z pálené hlíny, {v}. Tým {team} by je stejně rozšlapal. 🏺 /dokoupit",
    "{v}, tým {team} ti vystavil účet a bank ho zaplatil celý. 🧾 Nový bank za stovku: /dokoupit",
    "Kdyby se bank dal odhlásit z nemocenské, {v}… Tým {team} ho poslal k ledu. 🧊 /dokoupit",
    "{v}, prohra s týmem {team} je součást příběhu. Kapitola „dokup“ začíná na /dokoupit. 📖",
    "Koukám, {v}, že tě tým {team} pěkně vyškolil. 🚿 Bank 0, sprcha studená, /dokoupit teplý.",
    "{v}, tým {team} si z tvého banku udělal svačinu. 🥪 Dokup je za stovku: /dokoupit",
)


def _culprit(ticket: dict) -> str:
    """Tým, který hráči zkazil tiket: soupeř toho, na koho sázel v prvním
    prohraném legu (u remízy nebo sázky na remízu ten, kdo neprohrál/vyhrál)."""
    import generate_site

    for leg, win in zip(ticket["legs"], ticket["leg_wins"]):
        if win:
            continue
        m = leg["match"]
        home = m.get("home_short") or m["home"]
        away = m.get("away_short") or m["away"]
        out = generate_site.reg_outcome(m)
        if leg["market"] in ("1", "10"):
            return away
        if leg["market"] in ("2", "02"):
            return home
        return home if out == "1" else away  # sázka na remízu
    return "florbal"


def _state() -> dict:
    import generate_site

    season = json.load(open(tickets._p("season.json"), encoding="utf-8"))
    pub_path = tickets._p("published.json")
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    return generate_site.settle(season["matches"], published, tickets._p("bets.csv"))


def _live_persons(state: dict, rnd: int | None = None) -> set[str]:
    """Kdo má živý tiket (podaný v bets_sealed, nebo odhalený a čekající na dohrávku)."""
    out = {
        t["person"]
        for r, rows in state["open"].items()
        for t in rows
        if rnd is None or r == rnd
    }
    for t in tickets._load(tickets._p("bets_sealed.json"), []):
        if rnd is None or t["round"] == rnd:
            out.add(t["person"])
    return out


def broke_lines(state: dict, lost: list[dict]) -> list[str]:
    """Kdo těmito prohranými tikety přišel o poslední kredity (a nemá už nic
    živého): hláška s viníkem + pobídka k dokupu."""
    import hashlib

    first: dict[str, dict] = {}
    for t in lost:
        first.setdefault(t["person"], t)
    live = _live_persons(state)
    out = []
    for p in sorted(first):
        if state["banks"][p] < 1 and p not in live:
            t = first[p]
            i = int(hashlib.sha1(f"{t['round']}:{p}".encode()).hexdigest(), 16) % len(BROKE_LINES)
            out.append(
                BROKE_LINES[i].format(p=p, v=vokativ(p), g=genitiv(p), team=_culprit(t))
            )
    return out


def _ticket_key(t: dict) -> str:
    return f"{t['round']}:{t['person']}:{t['label']}"


def round_report(state: dict, rnd: int, new: list[dict]) -> list[str]:
    """Řádky vyhodnocení kola. Dohrané celé (nikdo v něm nemá živý tiket):
    ± všech členů za celé kolo. Rozehrané: jen tikety z `new` a kdo ještě čeká."""
    live = _live_persons(state, rnd)
    if not live:
        lines = [f"📊 Vyhodnocení {rnd}. kola:"]
        per = {p: 0.0 for p in state["banks"]}
        for t in state["settled"].get(rnd, []):
            per[t["person"]] += t["delta"]
    else:
        lines = [f"📊 {rnd}. kolo, zatím dohrané tikety:"]
        per = {}
        for t in new:
            per[t["person"]] = per.get(t["person"], 0.0) + t["delta"]
    for p, d in sorted(per.items(), key=lambda x: (-x[1], x[0])):
        mark = "✅" if d > 0 else ("❌" if d < 0 else "➖")
        lines.append(f"{mark} {p}: {d:+.0f}  (bank {state['banks'][p]:.0f})")
    if live:
        lines.append("⏳ Živý tiket: " + ", ".join(sorted(live)))
    return lines


def pending_report(mark: bool = True) -> str:
    """Nově vyhodnocené tikety od posledního hlášení (po kolech) + hlášky
    pro ty, co skončili na nule. Prázdný řetězec = nic nového.

    Bez data/reported.json (první start) se všechno dosud vyhodnocené jen
    zapíše jako ohlášené — historie se do chatu nesype."""
    state = _state()
    settled = [t for rows in state["settled"].values() for t in rows]
    path = tickets._p("reported.json")
    keys = sorted(_ticket_key(t) for t in settled)
    if not path.exists():
        path.write_text(json.dumps(keys))
        return ""
    seen = set(json.loads(path.read_text()))
    new = [t for t in settled if _ticket_key(t) not in seen]
    if not new:
        return ""
    if mark:
        path.write_text(json.dumps(keys))
    lines: list[str] = []
    for rnd in sorted({t["round"] for t in new}):
        if lines:
            lines.append("")
        lines.extend(round_report(state, rnd, [t for t in new if t["round"] == rnd]))
    broke = broke_lines(state, [t for t in new if not t["won"]])
    if broke:
        lines.append("")
        lines.extend(broke)
    return "\n".join(lines)


def results_summary() -> str:
    """/vysledky: stav posledního kola, které má něco vyhodnoceného."""
    state = _state()
    if not state["settled"] or not state["banks"]:
        return ""
    rnd = max(state["settled"])
    return "\n".join(round_report(state, rnd, state["settled"][rnd]))


def _published_rounds() -> set[int]:
    pub_path = DATA / "published.json"
    if not pub_path.exists():
        return set()
    return {v["round"] for v in json.loads(pub_path.read_text()).values()}


def run_update(force: bool = False, quiet: bool = False) -> str:
    """Spustí update.sh a vrátí, co se má poslat do chatu: nově vyhodnocené
    tikety a případně že je vypsané nové kolo. quiet=True (automatický běh)
    vrací prázdno, když není co hlásit."""
    if tickets.is_demo():
        import demo

        try:
            return demo.step()
        except Exception as exc:
            return f"❌ Zkušební update selhal: {exc}"
    before = _published_rounds()
    cmd = [str(ROOT / "update.sh")] + (["force"] if force else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        return "❌ Update selhal:\n" + "\n".join(tail)
    parts = []
    report = pending_report()
    if report:
        parts.append(report)
    new_rounds = _published_rounds() - before
    if new_rounds:
        parts.append(f"🎲 Vypsané {max(new_rounds)}. kolo — kurzy jsou na stránce.")
    if parts:
        return "\n\n".join(parts)
    return "" if quiet else "Nic nového — vypsané kolo se ještě hraje."


def update_due(now: datetime.datetime) -> bool:
    """Hraje se právě něco vypsaného, co by už mohlo mít výsledek?"""
    season = tickets._season()
    for m in tickets.open_matches(season, tickets._published()):
        start = tickets.deadline(m)
        if start + datetime.timedelta(seconds=CHECK_AFTER) <= now <= start + datetime.timedelta(seconds=CHECK_WINDOW):
            return True
    return False


def auto_tick(token: str, cfg: dict, clock: dict) -> None:
    """Automatika mezi zprávami: update po dohraných zápasech a hlášení
    tiketů, které mezitím vyhodnotil denní timer."""
    if not cfg.get("chat_id") or tickets.is_demo():
        return
    now = time.time()
    text = ""
    if now - clock.get("update", 0) >= CHECK_EVERY and update_due(datetime.datetime.now()):
        clock["update"] = now
        clock["report"] = now
        print("[auto] update po zápase", flush=True)
        text = run_update(quiet=True)
    elif now - clock.get("report", 0) >= REPORT_EVERY:
        clock["report"] = now
        text = pending_report()
    if text:
        print(f"[auto] hlásím:\n{text}", flush=True)
        send(token, cfg["chat_id"], text)


def is_admin(cfg: dict, username: str, user_id: int) -> bool:
    """Admin podle username NEBO telegram user_id (kdo nemá @username)."""
    admins = [str(a) for a in cfg.get("admins") or []]
    return not admins or username in admins or str(user_id) in admins


def handle(token: str, cfg: dict, msg: dict) -> None:
    # kód tiketu může přijít i jako popisek k obrázku/souboru — bereme i caption
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat_id = msg["chat"]["id"]
    sender = msg.get("from") or {}
    if not text:
        kind = ",".join(k for k in msg if k not in ("chat", "from", "date", "message_id"))
        print(f"[msg] chat={chat_id} from={sender.get('first_name', '')}: bez textu ({kind})", flush=True)
        return
    user_id = sender.get("id")
    username = sender.get("username") or ""
    private = msg["chat"].get("type") == "private"
    low = text.lower()
    print(
        f"[msg] chat={chat_id} from={sender.get('first_name', '')}"
        f" @{username} id={user_id}: {text if 'tip:' in low else text[:80]}",
        flush=True,
    )

    if not private and cfg.get("chat_id") and chat_id != cfg["chat_id"]:
        return

    person = tickets.person_for(
        user_id, username or sender.get("first_name") or str(user_id)
    )

    # kód tiketu ze stránky -> podání tiketu (funguje v DM i ve skupině);
    # platný tiket dostane jen ✅ reakci, ať se chat nespamuje
    m_tip = TIP_RE.search(text)
    if m_tip:
        ok, reply, _ = tickets.place_from_tip(user_id, person, m_tip.group(1))
        print(f"[tip] {person}: {'✅' if ok else '❌'} {reply}", flush=True)
        if ok:
            react(token, chat_id, msg["message_id"])
        else:
            send(token, chat_id, f"{vokativ(person)}, {reply[0].lower()}{reply[1:]}", msg["message_id"])
        return

    # jediné příkazy: update (admin), banky, výsledky a dokoupit (všichni);
    # cokoli jiného bot mlčky ignoruje
    word = low.lstrip("/").split()[0] if low.strip() else ""
    if "updatuj kurzy" in low or word == "update":
        if not is_admin(cfg, username, user_id):
            send(
                token,
                chat_id,
                "Update může spustit jen bookmaker 🎩",
                msg["message_id"],
            )
            return
        send(token, chat_id, run_update(force="force" in low), msg["message_id"])
    elif word == "banky":
        send(token, chat_id, banks_summary(), msg["message_id"])
    elif word == "dokoupit":
        reply = tickets.dokoupit(user_id, person)
        print(f"[dokup] {person}: {reply}", flush=True)
        send(token, chat_id, reply, msg["message_id"])
    elif word == "vysledky":
        send(
            token,
            chat_id,
            results_summary() or "Ještě není co vyhodnocovat.",
            msg["message_id"],
        )


def main() -> None:
    if not CONFIG.exists():
        raise SystemExit(
            "Chybí data/telegram.json — viz docstring (token od @BotFather, admins)."
        )
    cfg = json.loads(CONFIG.read_text())
    token = cfg["token"]
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    clock: dict[str, float] = {}
    print("Tipdivize bot běží, čekám na zprávy…")
    while True:
        try:
            auto_tick(token, cfg, clock)
        except Exception as exc:
            print("automatický update selhal:", exc, flush=True)
        try:
            resp = api(token, "getUpdates", timeout=30, offset=offset + 1)
        except Exception as exc:
            print("getUpdates selhal, zkusím znovu:", exc)
            time.sleep(10)
            continue
        for upd in resp.get("result", []):
            offset = max(offset, upd["update_id"])
            OFFSET.write_text(str(offset))
            if "message" in upd:
                try:
                    handle(token, cfg, upd["message"])
                except Exception as exc:
                    print("zpracování zprávy selhalo:", exc, flush=True)
            else:
                kind = ",".join(k for k in upd if k != "update_id")
                print(f"[upd] ignorováno: {kind}", flush=True)


if __name__ == "__main__":
    main()
