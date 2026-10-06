"""Dohnání tiketů, které bot nedostal (výpadek delší než 24 h — Telegram
starší zprávy zahodí). Dožene i /dokoupit z té doby (bez něj by tiket
hráče na nule neprošel).

Zdroj je export chatu z Telegram Desktopu (⋮ → Export chat history → JSON,
jen text) — soubor result.json. Každý „tip: …" kód podá jako by ho bot
dostal v čase odeslání zprávy: uzávěrka se počítá k tomu času, identita
podle odesílatele. Už podané kódy se přeskočí (stejný kód platí jen jednou).

    python3 dohnat_tikety.py result.json            # nanečisto, nic neuloží
    python3 dohnat_tikety.py result.json --podat    # podá

Musí běžet dřív než update, který kolo vyhodnotí (zápasy se skóre už
nejdou vsadit). Do chatu nic nepíše; stránku pak nasadí publish.sh.
"""

import datetime
import json
import pathlib
import shutil
import sys
import tempfile

import tickets
from telegram_bot import TIP_RE


def _text(msg: dict) -> str:
    t = msg.get("text", "")
    if isinstance(t, list):
        t = "".join(p if isinstance(p, str) else p.get("text", "") for p in t)
    return t


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    export = json.load(open(sys.argv[1], encoding="utf-8"))
    podat = "--podat" in sys.argv
    if not podat:
        # nanečisto nad kopií dat, ať se projeví i pořadí (dokup před tiketem)
        tmp = pathlib.Path(tempfile.mkdtemp()) / "data"
        shutil.copytree(tickets.DATA, tmp)
        tickets.DATA, tickets.PLAYERS = tmp, tmp / tickets.PLAYERS.name
    for msg in export.get("messages", []):
        text = _text(msg)
        m_tip = TIP_RE.search(text)
        dokup = text.strip().lower().startswith("/dokoupit")
        if not (m_tip or dokup) or not str(msg.get("from_id", "")).startswith("user"):
            continue
        user_id = int(msg["from_id"][4:])
        at = datetime.datetime.fromisoformat(msg["date"])
        person = tickets.person_for(user_id, msg.get("from") or str(user_id))
        if dokup:
            reply = tickets.dokoupit(user_id, person)
            ok = reply.startswith("✅")
        else:
            ok, reply, _ = tickets.place_from_tip(user_id, person, m_tip.group(1), now=at)
        print(f"{'✅' if ok else '❌'} {at:%d. %m. %H:%M} {person}: {reply}")
    if not podat:
        print("(nanečisto — nic se neuložilo; podat: --podat)")


if __name__ == "__main__":
    main()
