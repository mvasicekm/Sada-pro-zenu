# Monitor inzerátů Shimano 105 / GRX

Automatický monitor bazarů **Bazos.cz** a **Cyklobazar.cz**. Hledá komponenty
po kategoriích (páky, přehazovačka, přesmykač, kliky, kazeta, celá sada), aby
šlo poskládat celou skupinu **do rozpočtu 8 000 Kč**. Běží v GitHub Actions
**4× denně** (7, 12, 17 a 21 h), nové nálezy v limitu posílá e-mailem a pamatuje
si je v `seen_ids.json`. Všechny aktuální nálezy navíc vystavuje na webové
stránce s kalkulačkou sady.

## Soubory

| Soubor | Účel |
| --- | --- |
| `config.json` | kategorie, klíčová slova, cenové stropy, filtry, nastavení webů |
| `categories.py` | zařazení inzerátu ke kategorii + negativní filtry |
| `scraper.py` | parsery pro Bazoš a Cyklobazar, plánovač dotazů |
| `browser.py` | headless Chromium (Playwright) pro Cyklobazar |
| `mailer.py` | HTML/textový e-mail se štítky kategorií (`smtplib`) |
| `catalog.py` | trvalý katalog nálezů pro stránku (`docs/data.json`) |
| `main.py` | orchestrace: stáhni → zařaď → odfiltruj → pošli → ulož |
| `seen_ids.json` | paměť odeslaných inzerátů (commituje workflow) |
| `docs/` | statická stránka pro GitHub Pages (`index.html`, `app.js`, `style.css`) |
| `docs/data.json` | aktuální nálezy pro stránku (přepisuje monitor) |
| `docs/state.json` | tvoje ❤️ / ✗ volby (přepisuje stránka) |
| `.github/workflows/monitor.yml` | běh 4× denně + ruční spuštění |

## Jak funguje zařazení do kategorií

Inzerát se zařadí podle **titulku**, ne podle dotazu, který ho našel —
fulltext bazarů vrací spoustu nesouvisejících věcí. Pravidla:

1. **Globální filtr** vyhodí dětská kola, elektrokola, zapletená kola,
   poptávky („hledám“) apod. (`matching.global_exclude`).
2. **Relevance**: titulek musí zmiňovat `105`, `GRX` nebo některý kód modelu
   z configu. Tím vypadne Ultegra, XTR, SRAM…
3. **Zařazení**: titulek se porovná s `match_any` a `models` všech kategorií.
   - Při více shodách vyhrává **nejpřísnější cenový strop** — proto se
     inzerát s „přesmykač“ v názvu nespáruje s limitem pro páky.
   - Titulek se slovem „sada“/„grupa“ nebo shodou ve 3+ kategoriích
     spadne do **Celá sada** (limit 8 000 Kč).
4. **Per-kategorie `exclude`** vyhodí příslušenství (ořechy, bowdeny,
   destičky, wattmetry…), které se jen veze na stejném klíčovém slově.

Diakritika se ignoruje, takže „přesmykač“ i „presmykac“ fungují stejně.
Kódy modelů se porovnávají bez pomlček — `ST-R7000`, `ST R7000` i `str7000`.

### Cenové limity

| Kategorie | Limit |
| --- | --- |
| Celá sada | 8 000 Kč |
| Páky / Brzdy | 4 500 Kč |
| Přehazovačka | 1 500 Kč |
| Přesmykač | 700 Kč |
| Kliky | 2 200 Kč |
| Kazeta | 800 Kč |

Limity i klíčová slova se mění v `config.json`, kód se nedotýká.

## Stav obou webů

**Bazoš** běží plnohodnotně přes rychlý `requests` — všech 32 klíčových slov
při každém běhu (`query_mode: "all"`).

**Cyklobazar** je za Cloudflare a potřebuje skutečný prohlížeč, proto se tahá
headless Chromiem (Playwright + stealth). Platí ale důležité omezení:

> Cyklobazar pustí z automatizovaného prohlížeče spolehlivě **jen první
> načtení stránky**. Na další požadavky nasadí interaktivní Cloudflare
> Turnstile („Potvrďte, že jste člověk“), který čeká na kliknutí živého
> člověka. Skript tuto výzvu **vědomě neobchází** — detekuje ji, zaloguje
> a web pro daný běh přeskočí.

Prakticky to znamená, že z Cyklobazaru dostaneš při každém běhu první stránku
výsledků jednoho širokého dotazu (typicky ~25 nabídek). Proto je pro něj
nastavený režim `query_mode: "broad"` se dvěma dotazy, které se mezi běhy
**rotují** (`rotate_queries`), takže se postupně pokryje `shimano 105`
i `shimano grx`. Zařazení do kategorií dělá klasifikátor z titulků, takže
široký dotaz pokryje všechny kategorie najednou.

Pokud chceš z Cyklobazaru úplné pokrytí, legitimní cesty jsou:
- zapnout si hlídacího psa / e-mailové upozornění přímo v účtu na Cyklobazaru,
- zeptat se provozovatele na přístup k API nebo na povolení pro svého bota,
- nebo web vypnout přes `"cyklobazar": {"enabled": false}`.

## Stránka s kalkulačkou

Monitor po každém běhu zapíše všechny aktuální nálezy do `docs/data.json`
a workflow je commitne. GitHub Pages pak publikuje složku `docs/` jako
statickou stránku, kde jde u každého inzerátu kliknout ❤️ (chci) nebo ✗
(nechci) a kalkulačka sečte vybrané kusy proti rozpočtu 8 000 Kč.

Zapnutí: **Settings → Pages → Source: Deploy from a branch → branch `main`,
folder `/docs`**. Žádný deploy workflow není potřeba, nová data se publikují
automaticky s commitem monitoru.

Inzeráty, které se 14 dní neobjeví ve výsledcích (nejspíš prodané), katalog
sám odklidí. Hranici mění `catalog_retention_days` v `config.json`.

### Kde se berou tvoje volby

Kliknutí se **vždy** okamžitě uloží do `localStorage` prohlížeče. Když navíc
do *Nastavení* na stránce vložíš GitHub token, zapíší se i do
`docs/state.json` v repozitáři, takže je uvidíš i na jiném zařízení. Při
načtení se obě sady slučují a u každé položky vyhrává novější kliknutí.

Token vytvoříš takto:

1. https://github.com/settings/personal-access-tokens/new
2. *Repository access* → **Only select repositories** → `Sada-pro-zenu`
3. *Permissions → Repository permissions → Contents* → **Read and write**
4. Vygeneruj, zkopíruj a vlož na stránce do *Nastavení → Uložit a otestovat*

Token zůstává jen v tom prohlížeči a posílá se výhradně na `api.github.com`.
Dej mu krátkou expiraci a omez ho na tohle jedno repo — pak je nejhorší možný
následek jeho vyzrazení přepsání souborů v tomhle repozitáři. Tlačítko
*Zapomenout token* ho z prohlížeče smaže.

Protože titulky inzerátů píšou cizí lidé, vykresluje stránka veškerý text
přes `textContent` a odkazy propouští jen po kontrole, že vedou přes `https`
na `bazos.cz` nebo `cyklobazar.cz`. Bez toho by titulek inzerátu mohl
propašovat skript a token ukradnout.

## GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Popis | Příklad (Gmail) |
| --- | --- | --- |
| `SMTP_SERVER` | adresa SMTP serveru | `smtp.gmail.com` |
| `SMTP_PORT` | port (`587` = STARTTLS, `465` = SSL) | `587` |
| `SMTP_USERNAME` | přihlašovací jméno / e-mail | `tvuj.email@gmail.com` |
| `SMTP_PASSWORD` | **App Password**, ne běžné heslo | `abcd efgh ijkl mnop` |
| `EMAIL_TO` | příjemci, více oddělených čárkou | `ja@email.cz,ona@email.cz` |
| `EMAIL_FROM` | *(volitelné)* odesílatel, jinak `SMTP_USERNAME` | |

### Gmail App Password

1. Na Google účtu zapni dvoufázové ověření.
2. *Google Account → Security → 2-Step Verification → App passwords*.
3. Vytvoř heslo pro aplikaci „Mail“ a 16znakový kód vlož do `SMTP_PASSWORD`.

Workflow commituje `seen_ids.json` zpět do repozitáře, takže v
**Settings → Actions → General → Workflow permissions** musí být
*Read and write permissions*.

## Lokální test

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # jen kvůli Cyklobazaru

# 1) jen vypsat nálezy - nic se neposílá ani neukládá
python main.py --dry-run -v

# 2) jen jedna kategorie / jeden web (rychlé ladění)
python main.py --dry-run --only kazeta --site bazos

# 3) plný běh s odesláním e-mailu
cat > .env <<'EOF'
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=tvuj.email@gmail.com
SMTP_PASSWORD=app-password
EMAIL_TO=ja@email.cz
EOF
python main.py -v
```

`.env` je v `.gitignore`, do repozitáře se nedostane.

Přepínače: `--dry-run`, `--no-mail`, `--only KLIC` (lze opakovat),
`--site bazos|cyklobazar`, `--config`, `--seen`, `--data`, `-v`.

Stránku si lokálně prohlédneš přes `python -m http.server -d docs 8000`
a http://localhost:8000 (otevřít `index.html` přímo ze disku nestačí,
prohlížeč by zakázal načtení `data.json`).

`docs/data.json` se plní jen při **úplném** běhu — s `--dry-run`, `--only`
nebo `--site` se nepřepisuje, aby se katalog nezúžil na část nálezů.

## Konfigurace

```jsonc
{
  "total_budget": 8000,
  "allow_missing_price": false,     // true = posílat i "Dohodou"
  "sites": {
    "bazos":      { "enabled": true, "query_mode": "all",   "max_pages_per_query": 1 },
    "cyklobazar": { "enabled": true, "query_mode": "broad", "max_pages_per_query": 2 }
  },
  "broad_queries": ["shimano 105", "shimano grx"],
  "matching": {
    "bundle_min_matches": 3,        // kolik shod = "celá sada"
    "fallback_to_query_category": true,
    "relevance_any": ["105", "grx"],
    "global_exclude": ["detsk", "elektrokolo", "hledam", ...]
  },
  "categories": [
    { "key": "kazeta", "label": "Kazeta", "max_price": 800,
      "queries": ["shimano 105 kazeta", "cs-r7000"],
      "match_any": ["kazeta", "pastorky"],
      "models": ["cs-r7000", "cs-hg700"],
      "exclude": ["orech", "retez"] }
  ]
}
```

- `query_mode: "all"` projede všechna klíčová slova kategorií (rychlý Bazoš).
- `query_mode: "broad"` použije jen `broad_queries` (šetří požadavky).
- `browser.*` ladí headless prohlížeč (timeouty, prodlevy, `headless: false`
  pro vizuální ladění).

## Poznámky k provozu

- Parsery zkouší nejdřív známé CSS selektory a při redesignu webu padají na
  obecný fallback (odkazy na detail inzerátu + cena v okolí). Ověřeno tak, že
  i po odstranění **všech** `class` atributů z HTML se inzeráty najdou.
- Externí reklamní bannery ve výsledcích se zahazují (odkaz mimo doménu).
- E-mail se odesílá jen když přibyl aspoň jeden nový inzerát. Když odeslání
  selže, `seen_ids.json` se **neuloží**, aby se nález neztratil.
- Každý inzerát se tedy pošle právě jednou; stejné komponenty se v dalších
  e-mailech neopakují. Na stránce zůstávají, dokud se neprodají.
- Celý běh trvá ~1,5 minuty; krok ve workflow má limit 10 minut.
- GitHub cron se u vytížených runnerů může o pár minut zpozdit. Časy jsou
  v cronu uvedené v UTC ve dvou sadách (letní a zimní čas), takže se mimo
  sezónu můžou o hodinu posunout.
