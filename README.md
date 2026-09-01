# Tipdivize 🏑

Sázková kancelář pro florbalovou **Divizi mužů, skupinu B** (sezóna 2026/27) —
ligu, kde hraje **FbŠ Florbal Bohemians**. Nástupce Tipromile.

## Jak to funguje

1. **`scraper.py`** — stahuje data z ceskyflorbal.cz. Web je server-rendered,
   celý los (132 zápasů, 22 kol) je v jednom HTML na stabilní adrese
   `…/competition/detail/matches/8XM4?divisionAlias=8XM4-B&competitionFisId=<ročník>`
   (`8XM4-B` = Divize B, `competitionFisId` volí ročník; 2026/27 = `4822`).
   Parsují se bloky `<div class="Match">`. U odehraných zápasů s rozdílem
   jednoho gólu se stáhne i detail zápasu — tabulka třetin má sloupce
   „prodloužení"/„nájezdy" jen když k nim došlo, z čehož se pozná výsledek
   základní hrací doby. Detaily se cachují v `data/details/`. Čisté stdlib.
2. **`odds.py`** — kurzový model:
   - Elo přes všechny loňské zápasy Divize A–E + Národní ligy (dedup podle id,
     play-off série jsou ve více souborech). NL týmy startují na 1620 (odtud
     spadli Štíři ČB a Králův Dvůr), Divize na 1500, nováček z kraje
     (Soběslav) na 1400. Mezi sezónami regrese 30 % k priorům.
   - Očekávané góly z rozdílu Elo (koeficient fitovaný na loňsku) + tempo
     týmů; Poissonova mřížka → pravděpodobnosti výsledku základní hrací doby.
   - Trhy klasika na základní hrací dobu: **1** (výhra domácích), **10**
     (neprohra domácích), **02** (neprohra hostů), **2** (výhra hostů).
     **Na zápasy Bohemians jedině výhra Bohemky** — buď věříš, nebo nesázíš. Marže 8 %.
   - **Vypisuje se vždy jedno celé kolo** — další až po dohrání vypsaného
     (dohrávku přeskočí `--force`). Model se mezitím učí z výsledků, kurzy
     dalšího kola odrážejí formu.
   - Vypsaný kurz je zmrazený v `data/published.json` a nemění se
     (`--refresh` přepočítá jen neodehrané, používat vědomě).
3. **`generate_site.py`** — statický `index.html` se záložkami:
   - **Divize Sázky**: banky sázkařů, vypsané kolo s kurzy, náhled příštího
     kola, historie kol s vypořádanými tikety.
   - **Los a tabulka**: tabulka (bodování 3/2/1/0) a kompletní los.
4. **`telegram_bot.py`** — bot v sázkovém Telegram chatu: na „updatuj kurzy"
   od bookmakera spustí `update.sh` a pošle nově vypsané kolo; dál umí
   /kurzy, /banky, /chatid, /help. Nastavení je v docstringu souboru
   (token od @BotFather, **/setprivacy → Disable**, `data/telegram.json`
   je v .gitignore). Běží dlouhodobě, např. v tmux/systemd.

## Sázení — tajné tikety (commit–reveal)

- Každý začíná s bankem **1000**. Sólo tiket = jeden zápas; AKO = víc zápasů,
  kurzy se násobí, vyjít musí všechny. U zápasů Bohemians jedině výhra Bohemky.
- **Sází se klikáním na kurzy na stránce**: sestavíš tiket, zadáš vklad,
  „Zapečetit" — prohlížeč tiket zašifruje NaCl boxem veřejným klíčem
  bookmakera (tweetnacl z CDN) a vyplivne kód `tip: …`. Ten pošleš botovi
  do Telegramu (klidně do skupiny — je to šifra). **Identita = Telegram
  účet odesílatele**, žádná hesla.
- Bot kód dešifruje (`tickets.py`), ověří kurz/uzávěrku/bank, tiket uloží do
  `data/bets_sealed.json` (gitignored, vidí jen bookmaker) a veřejně
  publikuje jen SHA-256 otisk do `data/commitments.json` — na stránce visí
  „🔒 Kunc #a3f2c1". Platný tiket dostane v chatu jen **✅ reakci** (žádné
  zprávy navíc), chybný krátkou odpověď s důvodem.
- Po dohrání kola `python3 tickets.py` (součást update.sh) tikety **odhalí**:
  zapíše je do `data/bets.csv` a k otisku doplní obsah + nonce, takže si
  každý může hash přepočítat — nikdo (ani bookmaker) nemohl tiket zpětně
  změnit nebo přidat.
- V DM bot umí: `/moje` (mé zapečetěné tikety), `/storno` (vrátí tikety,
  u kterých nic nezačalo), `/bank`, `/kurzy`.
- Ruční tikety může bookmaker dál psát přímo do `bets.csv`
  (`round,person,ticket,match,market,stake`; stejný `ticket` = AKO).
- Vypořádání: podle základní hrací doby (prodloužení/nájezdy = remíza
  v základní době); výhra = vklad × (kurz − 1), prohra = −vklad.
- Klíče: `python3 keygen.py` (jednorázově) → `data/secret_key.txt`
  (gitignored, jen bookmaker) + `data/public_key.txt` (zabuduje se do
  stránky). Jména hráčů mapuje `data/players.json` (telegram id → jméno,
  bookmaker může přejmenovat).

## Denní použití

```sh
./update.sh   # scraper season → odds (vypíše kolo, když je čas) → index.html
```

…nebo napsat botovi „updatuj kurzy" do chatu. `scraper.py history` je
jednorázový (loňská data pro seed modelu).

## Data

- `data/season.json` — aktuální los + výsledky (přepisuje se při updatu)
- `data/details/*.json` — cache detailů zápasů (prodloužení/nájezdy)
- `data/history/*.json` — loňské soutěže (seed, stahují se jednou)
- `data/published.json` — **zmrazené vypsané kurzy, nikdy nemazat** (commituje se)
- `data/bets.csv` — odhalené/ruční tikety (zdroj vypořádání, commituje se)
- `data/commitments.json` — veřejné otisky tiketů (commituje se)
- `data/bets_sealed.json` — zapečetěné tikety (gitignored, jen bookmaker)
- `data/public_key.txt` / `data/secret_key.txt` — NaCl klíče (secret gitignored!)
- `data/players.json` — telegram id → jméno (gitignored)
- `data/telegram.json`, `data/telegram_offset.txt` — bot (gitignored, token!)

## TODO / nápady

- Ověřit detekci prodloužení na prvních reálných výsledcích sezóny.
- Web login (username+password) by chtěl opravdový server — zatím netřeba,
  identitu řeší Telegram účet.
