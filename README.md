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
     (neprohra domácích), **0** (čistá remíza v základní době), **02**
     (neprohra hostů), **2** (výhra hostů).
     **Na zápasy Bohemians jedině výhra Bohemky** — buď věříš, nebo nesázíš. Marže 8 %.
   - **Vypisuje se vždy jedno celé kolo** — další až po dohrání vypsaného
     (dohrávku přeskočí `--force`). Model se mezitím učí z výsledků, kurzy
     dalšího kola odrážejí formu.
   - Vypsaný kurz je zmrazený v `data/published.json` a nemění se
     (`--refresh` přepočítá jen neodehrané, používat vědomě).
3. **`generate_site.py`** — statický `index.html` se záložkami:
   - **Divize Sázky**: banky sázkařů se statistikou (tikety, úspěšnost, vsazeno,
     ROI, top výhra), kolik korun je reálně v banku a jak by se teď dělily,
     graf vývoje banků po kolech, vypsané kolo s kurzy (jen to, na které
     se právě sází — příští kolo se neukazuje, celý los je v druhé záložce),
     historie kol s vypořádanými tikety.
   - **Tikety**: všechny vyhodnocené tikety s filtry.
   - **Los a tabulka**: tabulka (bodování 3/2/1/0) a kompletní los.
   - **Informace**: pravidla pro hráče (vklad, dokupy, dělení banku), jak
     sázet, příkazy bota a co znamenají emoji.
4. **`telegram_bot.py`** — bot v sázkovém Telegram chatu: na „updatuj kurzy"
   od bookmakera spustí `update.sh`; dál umí jen /banky, /vysledky
   a /dokoupit (pro všechny), jiné zprávy ignoruje. **Sám do chatu píše jen
   tohle**: hlášku tomu, kdo skončil na nule (s pobídkou k dokupu), uznání za
   vyhrané AKO se 3+ zápasy a v pondělí v 9:00 vyhodnocení posledního kola.
   Všechno ostatní je jen na stránce. Co už hlásil, drží v `data/reported.json`
   (gitignored; při prvním startu si tam zapíše vše dosud vyhodnocené). Hráče
   oslovuje 5. pádem (`cestina.py`; jména/přezdívky jsou v `data/players.json`,
   nepravidelné tvary v `IRREGULAR`). Přejmenování hráče = přepsat jméno
   v players.json, bets.csv, topups.csv, commitments.json, archivu **i
   reported.json** (klíče tiketů nesou jméno; jinak bot hlášky pošle znovu). Nastavení je v docstringu souboru
   (token od @BotFather, **/setprivacy → Disable**, `data/telegram.json`
   je v .gitignore). Běží dlouhodobě, např. v tmux/systemd.

## Sázení — tajné tikety (commit–reveal)

- **Reálné peníze**: každý zaplatí 100 Kč a začíná s bankem **100 kreditů**
  (`START_BANK`, 1 kredit = 1 Kč). Sólo tiket = jeden zápas; AKO = víc zápasů,
  kurzy se násobí, vyjít musí všechny. U zápasů Bohemians jedině výhra Bohemky.
- **Dokupy**: kdo prohraje všechno, napíše do chatu `/dokoupit`. Bot ověří,
  že bank je na nule a hráč nemá živý tiket (podaný a nevyhodnocený, včetně
  čekání na dohrávku), a zapíše řádek do `data/topups.csv` (`round,person,credits,paid`):
  za dalších 100 Kč dalších 100 kreditů, kdykoliv a kolikrát chce. Kredity
  má hned (sloupec `round` jen říká, od kterého kola dokup figuruje v grafu).
  Když dokup nejde, bot odpoví „…, bank není 0, je …“ nebo „…, máš ještě živý tiket“. Na stránce se pak ukáže sloupec Dokupy.
  Kdo ve vyhodnocení kola skončí na nule, dostane od bota vtipnou hlášku
  s týmem, který mu to zkazil, a pobídkou k dokupu (`BROKE_LINES`).
- **Hraje se jen základní část.** Na jejím konci si celý bank (součet
  vložených korun, vybírají se až tehdy) rozdělí dva sázkaři s nejvyšším
  bankem v poměru svých banků (3000 : 1000 → ¾ : ¼).
  Stránka i `/banky` průběžně ukazují, kolik je v banku a jak by se teď dělil.
- Tikety 1. kola (hrané ještě s bankem 1000, před přechodem na reálný vklad)
  jsou vyřazené a archivované v `data/archiv/`.
- **Sází se klikáním na kurzy na stránce**: sestavíš tiket, zadáš vklad,
  „Zapečetit" — prohlížeč tiket zašifruje NaCl boxem veřejným klíčem
  bookmakera (tweetnacl z CDN) a vyplivne krátký kód `tip: …` (v2: binární payload, nonce odvozený
  z klíčů — sólo ~81 znaků; starší delší kódy bot pořád přijme). Ten pošleš botovi
  do Telegramu (klidně do skupiny — je to šifra). **Identita = Telegram
  účet odesílatele**, žádná hesla.
- Bot kód dešifruje (`tickets.py`), ověří kurz/uzávěrku/bank a tiket tím
  **podá**: uloží ho do `data/bets_sealed.json` (gitignored, vidí jen
  bookmaker) a veřejně publikuje jen SHA-256 otisk do `data/commitments.json`
  — na stránce visí „🔒 Kunc #a3f2c1". Podaný tiket dostane v chatu jen
  **✅ reakci** (žádné zprávy navíc), nepodaný krátkou odpověď s důvodem.
- **Pojmy** (stejně na stránce v Informacích): *zapečetěný* = kód vyrobený
  v prohlížeči, ještě nic neplatí; *podaný* = bot dal ✅, vklad je odečtený;
  *živý* = podaný a nevyhodnocený; *vyhodnocený* = po dohrání všech zápasů.
- Po dohrání kola `python3 tickets.py` (součást update.sh) tikety **odhalí**:
  zapíše je do `data/bets.csv` a k otisku doplní obsah + nonce, takže si
  každý může hash přepočítat — nikdo (ani bookmaker) nemohl tiket zpětně
  změnit nebo přidat.
- Ruční tikety může bookmaker dál psát přímo do `bets.csv`
  (`round,person,ticket,match,market,stake`; stejný `ticket` = AKO).
- Vypořádání: podle základní hrací doby (prodloužení/nájezdy = remíza
  v základní době); výhra = vklad × (kurz − 1), prohra = −vklad.
- **Dohrávky**: tiket se odhalí a vyhodnotí, až jsou dohrané VŠECHNY jeho
  zápasy — tiket s odloženým zápasem zůstává živý („⏳ čeká na dohrávku")
  a vklad zůstává blokovaný. Nové kolo se vypíše normálním updatem (odložený
  zápas ho neblokuje). Dohrávka se **sází spolu s aktuálním kolem**, pokud se
  hraje dřív než první zápas příštího kola (`dohravka_bettable`; cokoliv
  před 3. kolem patří do okna 2. kola); kurz zůstává ten původně vypsaný.
  Dohrávka s pozdějším nebo neznámým termínem se otevře až s dalším kolem. Tiket s dohrávkou patří do
  aktuálního kola (v bets.csv má jeho číslo, zápas je podle id).
- Klíče: `python3 keygen.py` (jednorázově) → `data/secret_key.txt`
  (gitignored, jen bookmaker) + `data/public_key.txt` (zabuduje se do
  stránky). Jména hráčů mapuje `data/players.json` (telegram id → jméno,
  bookmaker může přejmenovat).

## Bot musí běžet

Jediná závislost mimo stdlib je **pynacl** (dešifrování tiketů). Bot běží
z projektového venv: `python3 -m venv --system-site-packages .venv &&
.venv/bin/pip install pynacl` (`.venv/` je v .gitignore). Bez pynacl bot
každý tiket odmítne s „Kód tiketu se nepodařilo rozbalit“.

Bot je obyčejný proces — když neběží, tikety v chatu nikdo nepřijme (Telegram
je drží 24 h, po startu je bot dožene). Trvalé spuštění je přes systemd user
service `tipdivize-bot.service` (návod v hlavičce souboru: `systemctl --user
enable --now tipdivize-bot` + `loginctl enable-linger`). Nouzově stačí
`tmux new -d -s tipbot '.venv/bin/python3 -u telegram_bot.py >> .bot.log 2>&1'`.
Musí běžet **jen jedna instance** — dvě se perou o token (HTTP 409 v logu).
Bot importuje `tickets.py`/`generate_site.py` jen při startu, po změně kódu
ho restartuj. Zprávy bez textu (samotný obrázek) ignoruje a zaloguje; kód
tiketu pošli jako text nebo jako popisek k obrázku.

## Update běží automaticky

Systemd user timer `tipdivize-update.timer` spouští `update.sh` **ve všední
den v 8 a ve 20, o víkendu ve 12, 17, 20:30 a 23** (instalace v hlavičce
souboru timeru) — vyhodnocené tikety jsou na stránce brzy po zápase. Los na ceskyflorbal.cz se
mění — přesuny zápasů, doplněné časy, výsledky — a bez pravidelného scrapu by
stránka ukazovala starý termín. Commit + push + Render deploy vznikne jen když
se data opravdu změní (samotné razítko `scraped_at` se zahazuje). `update.sh`
drží zámek `.update.lock`, takže se timer, bot a ruční spuštění nepoperou.
Log: `.update.log`; stav: `systemctl --user list-timers`. Když je potřeba mít
výsledky dřív, stačí botovi napsat „updatuj kurzy".

Ručně kdykoli:

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
- `data/topups.csv` — dokupy (kolo, sázkař, kredity, zaplaceno Kč; commituje se)
- `data/archiv/` — vyřazené tikety 1. kola z doby banku 1000
- `data/commitments.json` — veřejné otisky tiketů (commituje se)
- `data/bets_sealed.json` — podané, ještě neodhalené tikety (gitignored, jen bookmaker)
- `data/public_key.txt` / `data/secret_key.txt` — NaCl klíče (secret gitignored!)
- `data/players.json` — telegram id → jméno (gitignored)
- `data/telegram.json`, `data/telegram_offset.txt` — bot (gitignored, token!)
- `data/reported.json` — tikety, které bot už ohlásil do chatu (gitignored)

## TODO / nápady

- Ověřit detekci prodloužení na prvních reálných výsledcích sezóny.
- Web login (username+password) by chtěl opravdový server — zatím netřeba,
  identitu řeší Telegram účet.
