"""Telegram bot Tipdivize — člen sázkového chatu.

Poslouchá skupinu přes oficiální Bot API (long polling, čisté stdlib) a umí:
  „tip: …" kód ze stránky   — přijme tajný tiket; platný dostane jen ✅
                               reakci (žádný spam), chybný krátkou odpověď
  „updatuj kurzy" / /update  — spustí ./update.sh a pošle, co se stalo
                               (jen pro adminy z configu)
  /kurzy                     — vypsané kolo s kurzy
  /banky                     — stav bank sázkařů
  /chatid                    — vypíše id chatu (pro prvotní nastavení)

Nastavení (jednorázově):
  1. U @BotFather: /newbot -> token; /setprivacy -> Disable (jinak bot ve
     skupině nevidí obyčejné zprávy, jen /příkazy).
  2. Přidat bota do skupiny.
  3. Vytvořit data/telegram.json (je v .gitignore!):
     {"token": "123:ABC", "chat_id": null, "admins": ["mschejbal"]}
     chat_id null = bot reaguje všude; po /chatid ho doplň, ať reaguje
     jen ve vašem chatu.
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


def round_summary() -> str:
    """Vypsané kolo s kurzy (z published.json + season.json; v demo režimu demo_*)."""
    season = json.load(open(tickets._p("season.json"), encoding="utf-8"))
    pub_path = tickets._p("published.json")
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    by_id = {m["id"]: m for m in season["matches"]}
    open_entries = [
        v
        for v in published.values()
        if not (by_id.get(v["match_id"]) or {}).get("score")
    ]
    if not open_entries:
        return "Žádné kolo není vypsané."
    rnd = min(e["round"] for e in open_entries)
    lines = [f"🏑 Vypsané {rnd}. kolo:"]
    entries = [e for e in open_entries if e["round"] == rnd]
    entries.sort(key=lambda v: (by_id[v["match_id"]]["date"] or "", v["match_id"]))
    for v in entries:
        m = by_id[v["match_id"]]
        o = v["odds"]
        cols = "  ".join(
            f"{mk}: {o[mk]:.2f}" for mk in ("1", "10", "02", "2") if mk in o
        )
        star = " ⭐ (jen výhry!)" if v.get("special") else ""
        lines.append(f"{m['date']} {m['home']} – {m['away']}\n   {cols}{star}")
    return "\n".join(lines)


def banks_summary() -> str:
    import generate_site

    season = json.load(open(tickets._p("season.json"), encoding="utf-8"))
    pub_path = tickets._p("published.json")
    published = json.loads(pub_path.read_text()) if pub_path.exists() else {}
    state = generate_site.settle(season["matches"], published, tickets._p("bets.csv"))
    if not state["banks"]:
        return f"Zatím nikdo nesází. Každý začíná s bankem {generate_site.START_BANK}."
    lines = ["💰 Banky:"]
    for i, (p, b) in enumerate(sorted(state["banks"].items(), key=lambda x: -x[1]), 1):
        lines.append(f"{i}. {p}: {b:.0f} ({b - generate_site.START_BANK:+.0f})")
    return "\n".join(lines)


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
    return "\n".join(lines)


def _published_rounds() -> set[int]:
    pub_path = DATA / "published.json"
    if not pub_path.exists():
        return set()
    return {v["round"] for v in json.loads(pub_path.read_text()).values()}


def run_update() -> str:
    if tickets.is_demo():
        import demo

        try:
            return demo.step()
        except Exception as exc:
            return f"❌ Zkušební update selhal: {exc}"
    before = _published_rounds()
    proc = subprocess.run(
        [str(ROOT / "update.sh")], capture_output=True, text=True, cwd=ROOT, timeout=600
    )
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
    text = (msg.get("text") or "").strip()
    if not text:
        return
    chat_id = msg["chat"]["id"]
    sender = msg.get("from") or {}
    user_id = sender.get("id")
    username = sender.get("username") or ""
    private = msg["chat"].get("type") == "private"
    low = text.lower()
    print(
        f"[msg] chat={chat_id} from={sender.get('first_name', '')}"
        f" @{username} id={user_id}: {text[:80]}",
        flush=True,
    )

    if low.startswith("/chatid"):
        send(token, chat_id, f"chat_id: {chat_id}", msg["message_id"])
        return
    if not private and cfg.get("chat_id") and chat_id != cfg["chat_id"]:
        return

    person = tickets.person_for(
        user_id, username or sender.get("first_name") or str(user_id)
    )

    # /demo start|stop — zkušební liga (jen bookmaker)
    if low.startswith("/demo"):
        if not is_admin(cfg, username, user_id):
            send(
                token,
                chat_id,
                "Zkušební ligu ovládá jen bookmaker 🎩",
                msg["message_id"],
            )
            return
        import demo

        arg = (low.split() + [""])[1]
        try:
            if arg == "start":
                send(token, chat_id, demo.start())
            elif arg == "stop":
                send(token, chat_id, demo.stop())
            else:
                send(token, chat_id, "Použij /demo start nebo /demo stop.")
        except Exception as exc:
            send(token, chat_id, f"❌ /demo {arg} selhalo: {exc}")
        return

    # zapečetěný tiket ze stránky (funguje v DM i ve skupině);
    # platný tiket dostane jen ✅ reakci, ať se chat nespamuje
    m_tip = TIP_RE.search(text)
    if m_tip:
        ok, reply, _ = tickets.place_from_tip(user_id, person, m_tip.group(1))
        if ok:
            react(token, chat_id, msg["message_id"])
        else:
            send(token, chat_id, f"{person}: {reply}", msg["message_id"])
        return

    if private:
        if low.startswith("/moje") or low == "moje":
            send(token, chat_id, tickets.my_tickets(user_id, person))
        elif low.startswith("/storno") or low == "storno":
            send(token, chat_id, tickets.storno(user_id))
        elif low.startswith("/bank"):
            send(
                token, chat_id, f"K dispozici máš {tickets.available_bank(person):.0f}."
            )
        elif low.startswith("/kurzy"):
            send(token, chat_id, round_summary())
        else:
            send(
                token,
                chat_id,
                "Ahoj! Tiket si naklikej na stránce a pošli mi kód „tip: …“ "
                "(sem, nebo do skupiny — je šifrovaný). Dál umím: /moje (tvoje "
                "zapečetěné tikety), /storno, /bank, /kurzy.",
            )
        return

    if "updatuj kurzy" in low or low.startswith("/update"):
        if not is_admin(cfg, username, user_id):
            send(
                token,
                chat_id,
                "Update může spustit jen bookmaker 🎩",
                msg["message_id"],
            )
            return
        send(token, chat_id, run_update(), msg["message_id"])
    elif low.startswith("/kurzy"):
        send(token, chat_id, round_summary(), msg["message_id"])
    elif low.startswith("/banky"):
        send(token, chat_id, banks_summary(), msg["message_id"])
    elif low.startswith("/vysledky"):
        send(
            token,
            chat_id,
            results_summary() or "Ještě není co vyhodnocovat.",
            msg["message_id"],
        )
    elif low.startswith("/help"):
        send(
            token,
            chat_id,
            "Umím: „updatuj kurzy“ (bookmaker), /kurzy, /banky, /vysledky, "
            "/chatid, /help. Tiket pošli jako „tip: …“ kód ze stránky "
            "(klidně sem — je šifrovaný).",
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
                    print("zpracování zprávy selhalo:", exc)


if __name__ == "__main__":
    main()
