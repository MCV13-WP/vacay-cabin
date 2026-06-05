# 🏡 Project Vacay Cabin

Automatische scraper die recreatieve vakantiewoningen **te koop** in Nederland dagelijks opspoort, filtert en publiceert op een persoonlijke website — met e-mailmelding bij nieuwe vondsten.

## Wat doet het?

- Scrapt dagelijks **14 bronnen** (makelaars, parkwebsites, advertentiesites)
- Filtert op prijs, regio, slaapkamers en personencapaciteit
- Detecteert nieuwe woningen en markeert verkochte of offline listings
- Publiceert resultaten op een **GitHub Pages website** met kaart, zoekfunctie en favorieten
- Stuurt een **dagelijkse e-mail** bij nieuwe woningen, inclusief foto en prijstrend
- Draait volledig automatisch via **GitHub Actions** om 07:00 NL-tijd

---

## Zoekcriteria

| Criterium | Waarde |
|---|---|
| Type | Recreatiewoning op vakantiepark |
| Max. prijs | € 350.000 |
| Min. slaapkamers | 4 |
| Min. personen | 8 |
| Regio's | Veluwe, Gelderland, Overijssel, Drenthe, Utrecht, Noord-Brabant, Limburg |

---

## Scraper-bronnen (14)

Recreatievastgoed.nl · Marktplaats · VakantiehuisTekoop.nl · Landal Makelaardij · RecreatiewoningenTekoop.nl · EuroParcs Makelaardij · Veluwechalets.nl · UwTweedeHuisMakelaar.nl · UwBuitenleven.nl · TopParkenVerkoop.nl · Vakantiemakelaar.nl · CenterParcs Vastgoed · Jaap.nl · Huislijn.nl

---

## Projectstructuur

```
vacay-cabin/
├── scraper.py              # Hoofdscript (~1.670 regels, 14 scrapers + logica)
├── config.py               # ⚠️ Niet in git — gegenereerd via GitHub Secrets
├── requirements.txt        # Python dependencies
├── known_listings.json     # ⚠️ Niet in git — persistente database van bekende woningen
├── docs/
│   ├── index.html          # Website (GitHub Pages)
│   └── data.json           # Datakoppeling scraper ↔ website
└── .github/
    └── workflows/
        └── daily-scraper.yml  # GitHub Actions cron
```

---

## Website

De website (GitHub Pages) toont alle gevonden woningen via vier tabs:

| Tab | Inhoud |
|---|---|
| ⭐ Nieuw | Alleen woningen gevonden in de laatste run |
| 🏘 Alle woningen | Volledig aanbod inclusief offline en verkochte woningen |
| ❤️ Favorieten | Persoonlijk opgeslagen woningen |
| 🗺️ Kaart | OpenStreetMap met gekleurde markers per status |

Per woning wordt getoond: foto, bron, titel, prijs met trend (▲/▼), slaapkamers, personen, locatie en eerste verschijningsdatum.

---

## E-mailmelding

Bij nieuwe woningen verstuurt het script automatisch een HTML-e-mail met een overzicht van de nieuwe vondsten en een directe link naar de website. Bij 14+ dagen zonder nieuwe woningen volgt een signaalmail.

---

## Automatisering

Het script draait dagelijks via GitHub Actions (07:00 NL-tijd). Na elke run worden de resultaten gecommit en gepusht naar GitHub Pages.

Benodigde GitHub Secrets: `SMTP_HOST` · `SMTP_PORT` · `SMTP_USER` · `SMTP_PASSWORD` · `EMAIL_FROM` · `EMAIL_TO`

---

## Veiligheid

Wachtwoorden en configuratie worden **nooit gecommit**. `config.py` wordt uitsluitend gegenereerd vanuit GitHub Secrets tijdens de Actions-run.

---

*Gebouwd met Python, BeautifulSoup, Playwright en GitHub Actions. Ontwikkeld samen met Claude (Anthropic).*
