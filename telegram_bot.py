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

import json
import pathlib
import re
import subprocess
import time
import urllib.parse
import urllib.request

import tickets

TIP_RE = re.compile(r"tip:\s*([A-Za-z0-9+/=]{40,})")

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
CONFIG = DATA / "telegram.json"
OFFSET = DATA / "telegram_offset.txt"

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
    for i, (p, b) in enumerate(sorted(banks.items(), key=lambda x: (-x[1], x[0])), 1):
        lines.append(f"{i}. {p}: {b:.0f}")
    return "\n".join(lines)


BROKE_LINES = (
    "Tyjo, to byla fakt smůla, {p}. {team} je kousavá potvůrka. 🐾 Co takhle si dokoupit další dukáty? /dokoupit",
    "{p}, tým {team} ti sebral poslední dukáty. 💸 Nevadí, mincovna má otevřeno: /dokoupit",
    "Au, {p}. Tým {team} ti vybral bank do posledního dukátu. 🪙 Za stovku nová truhla: /dokoupit",
    "{p}, tohle bolelo. Tým {team} zařídil nulu na kontě. Dukáty se dají dokoupit, hrdost ne. 🛡️ /dokoupit",
    "{p} je na nule a může za to {team}. 🏑 Doplň dukáty a vrať jim to: /dokoupit",
    "Smůla, {p}. Tým {team} dneska kousal. Truhla je prázdná, ale /dokoupit ji naplní. 🪙",
    "{p}, tým {team} ti sfoukl poslední dukát. 🕯️ Nová stovka, nový bank: /dokoupit",
    "{p} je bez dukátů, tým {team} byl bez slitování. 🧾 /dokoupit a jde se znovu.",
    "{p}, bank 0. Tým {team} se prostě nezeptal. 🤷 Dukáty na dokoupení jsou za stovku: /dokoupit",
    "Kdo by to od týmu {team} čekal, že, {p}? 😅 Poslední dukát je pryč, ale /dokoupit tě vrátí do hry.",
    "{p}, tým {team} ti právě ukázal, proč se říká „florbal je nevyzpytatelný“. 🎲 Stovka na stůl: /dokoupit",
    "Bank hráče {p}: 0. Nálada hráče {p}: taky. 📉 Tým {team} se omlouvá, bookmaker ne. /dokoupit",
    "{p}, tým {team} tě poslal do šatny s prázdnou kapsou. 🧦 Dukáty čekají ve výdejně: /dokoupit",
    "{p} zavírá krám, tým {team} vyprodal zásoby. 🏪 Znovu otevřeno po /dokoupit.",
    "Tým {team} dneska hrál jako o život a {p} to odnesl. 🚑 První pomoc: /dokoupit",
    "{p}, nula je jen začátek každého velkého comebacku. 🔄 Tým {team} tě jen rozehřál. /dokoupit",
    "Gratulace, {p}, máš první čistý bank sezóny. Zásluhu si připisuje tým {team}. 🧼 /dokoupit",
    "{p}, tým {team} si vzal tvé dukáty a nevrátí je. 🏴‍☠️ Ale stovka koupí novou loď: /dokoupit",
    "Někdy vyhraješ, někdy hraje tým {team}. {p} dneska zažil to druhé. 🤕 /dokoupit",
    "{p}, bank na nule, hlava vzhůru. Tým {team} to nemyslel osobně. 🫂 /dokoupit",
    "Tým {team} právě sfoukl bank hráče {p} jako svíčku na dortu. 🎂 Přání: /dokoupit",
    "{p}, výsledek dnes napsal tým {team} a tvůj bank to nepřežil. ✍️ Nová kapitola: /dokoupit",
    "Tabulka banků má novou nulu: {p}. Sponzorem je tým {team}. 🏷️ Odsponzoruj se zpátky: /dokoupit",
    "{p}, tvé dukáty odjely s autobusem týmu {team}. 🚌 Další spoj jede po /dokoupit.",
    "Tým {team} ti dal lekci, {p}. Školné bylo sto korun, opakovačka taky: /dokoupit 🎓",
    "{p}, mezi tebou a bankem 0 už nic nestojí. Postaral se tým {team}. 🧱 /dokoupit a stav znovu.",
    "Ještě že dukáty nejsou z pálené hlíny, {p}. Tým {team} by je stejně rozšlapal. 🏺 /dokoupit",
    "{p}, tým {team} ti vystavil účet a bank ho zaplatil celý. 🧾 Nový bank za stovku: /dokoupit",
    "Kdyby se bank dal odhlásit z nemocenské, {p}… Tým {team} ho poslal k ledu. 🧊 /dokoupit",
    "{p}, prohra s týmem {team} je součást příběhu. Kapitola „dokup“ začíná na /dokoupit. 📖",
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


def broke_lines(state: dict, rnd: int) -> list[str]:
    """Kdo v tomto kole prohrál poslední kredity: hláška s viníkem + pobídka k dokupu."""
    import hashlib

    lost: dict[str, dict] = {}
    for t in state["settled"][rnd]:
        if not t["won"]:
            lost.setdefault(t["person"], t)
    out = []
    for p in sorted(lost):
        if state["banks"][p] < 1:
            i = int(hashlib.sha1(f"{rnd}:{p}".encode()).hexdigest(), 16) % len(BROKE_LINES)
            out.append(BROKE_LINES[i].format(p=p, team=_culprit(lost[p])))
    return out


def results_summary() -> str:
    """Vyhodnocení posledního dohraného kola: všichni členové a jejich ±."""
    import generate_site

    season = json.load(open(tickets._p("season.json"), encoding="utf-8"))
    pub_path = tickets._p("published.json")
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    state = generate_site.settle(season["matches"], published, tickets._p("bets.csv"))
    if not state["settled"] or not state["banks"]:
        return ""
    rnd = max(state["settled"])
    per = {p: 0.0 for p in state["banks"]}
    for t in state["settled"][rnd]:
        per[t["person"]] = per.get(t["person"], 0.0) + t["delta"]
    lines = [f"📊 Vyhodnocení {rnd}. kola:"]
    for p, d in sorted(per.items(), key=lambda x: (-x[1], x[0])):
        mark = "✅" if d > 0 else ("❌" if d < 0 else "➖")
        lines.append(f"{mark} {p}: {d:+.0f}  (bank {state['banks'][p]:.0f})")
    broke = broke_lines(state, rnd)
    if broke:
        lines.append("")
        lines.extend(broke)
    return "\n".join(lines)


def _published_rounds() -> set[int]:
    pub_path = DATA / "published.json"
    if not pub_path.exists():
        return set()
    return {v["round"] for v in json.loads(pub_path.read_text()).values()}


def run_update(force: bool = False) -> str:
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
    # do chatu jde jen vyhodnocení — výsledky a nové kurzy jsou na stránce
    if _published_rounds() - before:
        return results_summary() or "Nové kolo vypsáno — kurzy jsou na stránce."
    return "Vypsané kolo ještě není dohrané — vyhodnocení přijde po posledním zápase."


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
            send(token, chat_id, f"{person}: {reply}", msg["message_id"])
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
        send(token, chat_id, f"{person}: {reply}", msg["message_id"])
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
    print("Tipdivize bot běží, čekám na zprávy…")
    while True:
        try:
            resp = api(token, "getUpdates", timeout=50, offset=offset + 1)
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
