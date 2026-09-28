# BlackLabs Watchdog pre Home Assistant

## Inštalácia cez HACS

Vyžaduje nainštalovaný HACS. V Home Assistante otvorte **HACS → ⋮ → Custom repositories**, pridajte `https://github.com/petosiso/HomeFleet-HA-integration` ako typ **Integration** a stiahnite **BlackLabs Watchdog**. Reštartujte Home Assistant a v **Nastavenia → Zariadenia a služby → Pridať integráciu** vyberte **BlackLabs Watchdog**.

Repozitár nemusí byť zaradený do predvoleného katalógu HACS; stačí ho pridať ako vlastný repozitár. Vydania používajú GitHub Releases s tagmi `vX.Y.Z`, takže HACS zobrazí verzie a upozorní na novšie vydanie. Novú verziu nainštalujte cez HACS a potom reštartujte HA.

## Kompatibilita s Home Assistant Core a logo

Primárnym vývojovým cieľom je HA Core **2026.9.3**. Na inštalácii s **2026.2.2** bolo pozorované odosielanie reportov, ale zber add-onov vo verzii Watchdog 0.1.2 zlyhával pre chýbajúcu funkciu `get_addons_list`. Od Watchdog 0.1.3 sa používa detekcia dostupnej funkcie: novší HA poskytne `get_addons_list`, starší HA poskytne zoznam add-onov cez `get_supervisor_info`. Kód neporovnáva textové čísla verzií HA. Zlyhanie jedného zdroja zostáva obmedzené na jeho kategóriu. Regresné testy overujú obe vetvy, ale úplné testy so skutočným HA 2026.2.2 a 2026.9.3 ani živá kontrola po oprave ešte neprebehli. Ostatné verzie HA preto nepovažujte za overené.

Značka BlackLabs je v `custom_components/homefleet/brand/`: štvorcová ikonka a široké logo vychádzajú z rovnakých podkladov ako HomeFleet aplikácia. [HA Core od 2026.3](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/) načíta tieto súbory priamo z integrácie. HA Core 2026.2.2 lokálne značky ešte nepodporuje, preto sa tam ikonka zobrazí až po aktualizácii HA na 2026.3 či novší. [HACS 2.0.5 môže na svojej obrazovke naďalej ukazovať zástupnú ikonku](https://github.com/hacs/integration/issues/5223), aj keď samotná HA integrácia zobrazuje logo správne. Po aktualizácii integrácie reštartujte HA Core; obrázky môže prehliadač ďalej držať v cache.

## Ručná inštalácia

Skopírujte `custom_components/homefleet` do konfiguračného adresára Home Assistanta a reštartujte HA. V **Nastavenia → Zariadenia a služby** pridajte integráciu **BlackLabs Watchdog**. Zadajte HTTPS adresu HomeFleet backendu a existujúci 64-znakový `integration_key` príslušnej inštalácie. Vyberte sledované entity a interval odosielania (predvolene 5 minút, povolené 1 až 1440 minút). V možnostiach možno zmeniť entity a interval; cez opätovnú konfiguráciu adresu a kľúč. Neplatný interval uložený staršou verziou pri spustení použije predvolených 5 minút a zaloguje upozornenie.

Integrácia odosiela aktuálny report do `POST /api/ha-integration/lifechecks`. Pri zlyhaní ho zahodí; ďalší interval zozbiera nové dáta. Fronta sa neukladá. Keď HA nebeží, reporty nevznikajú.

Inventár zahŕňa jeden záznam na doménu nakonfigurovaných HA integrácií, nainštalované add-ony pri dostupnom Supervisorovi a nainštalované HACS repozitáre pri spustenom HACS. Interné komponenty a závislosti načítané cez `default_config`, napríklad `sensor`, `auth` alebo `webhook`, sa neposielajú. Integrácie nakonfigurované iba cez YAML bez `ConfigEntry` sa tiež neposielajú. Neprítomný voliteľný zdroj dáva prázdnu kategóriu; chyba pri čítaní existujúceho zdroja označí report ako neúplný. Metadáta integrácií sa čítajú iba z HA cache, reportovanie nespúšťa ich načítavanie. Nakonfigurovaná, ale práve nenačítaná integrácia sa odošle ako doména bez verzie a nepovažuje sa za chybu. Iná chyba manifestu uvedie v `errorMessage` doménu, bezpečný typ výnimky a informáciu, či sa podarilo odoslať náhradný záznam. Verzia Supervisora sa číta z poľa `supervisor` jeho súhrnných údajov.

Chyby sa izolujú po položkách. Chybná entita, HACS repozitár alebo add-on sa vynechá; už získané aj nasledujúce platné položky sa odošlú. Verzie Supervisora a OS sa získavajú nezávisle od add-onov. Aj chyba iterovania celého zdroja zachová položky, ktoré už boli zozbierané. Report dostane `isComplete = false` a stručný `errorMessage` s kategóriou a poradím chybnej položky, bez surových hodnôt či textu výnimiek. Zobrazí sa najviac 20 detailov a počet ostatných chýb.

Zdroje sa zbierajú súbežne, každý s vlastným limitom 10 sekúnd. Pomalý zdroj preto neodkladá spustenie ostatných. Celý zber má limit 20 sekúnd a HTTP požiadavka 30 sekúnd. Pokus o zostavenie a odoslanie má ešte celkový limit 55 sekúnd. Pri timeoutoch zberu sa odošle dostupná časť reportu. Pri prekročení celkového limitu pokusu sa pokus zahodí a ďalší interval začne nový. Pri ukončení zberu alebo unload sa zrušia a dokončia všetky jeho podúlohy. Limity sú kooperatívne: blokujúci synchrónny kód cudzej integrácie v event loope nemožno násilne prerušiť. Zber preto používa cached HA dáta, bez vlastného načítavania manifestov, a medzi položkami odovzdáva riadenie event loopu.

HACS adaptér číta `repositories.list_all` a kontroluje `data.installed` samostatne pri každom repozitári. Add-ony na HA 2026.2.2 číta cez `get_supervisor_info`; na HA 2026.9.3 cez `get_addons_list`. Ak hromadná konverzia v novšom helperi zlyhá, záložná cesta číta rovnakú cache cez `hassio.const.DATA_ADDONS_LIST` a volá `to_dict()` po jednotlivých modeloch. Táto záloha závisí od interného rozhrania HA 2026.9.3; pri jeho zmene zostáva zlyhanie obmedzené na kategóriu add-onov. Rozhrania boli porovnané so [zdrojom HA 2026.2.2](https://github.com/home-assistant/core/blob/2026.2.2/homeassistant/components/hassio/coordinator.py), [zdrojom HA 2026.9.3](https://github.com/home-assistant/core/blob/2026.9.3/homeassistant/components/hassio/coordinator.py) a [HACS runtime](https://github.com/hacs/integration/blob/main/custom_components/hacs/base.py).

Backendový limit zostáva 1 MiB (1 048 576 bajtov). Klient pred odoslaním zmeria presné UTF-8 bajty JSON. Ak sa report nezmestí, ponechá verzie, nastaví `isComplete = false`, do `errorMessage` zapíše prekročenú veľkosť a počty vynechaných položiek a odošle prázdne zoznamy entít aj inventára. Zachová tiež obmedzenú časť predchádzajúceho popisu chýb. Ide stále o jediný POST; veľký report sa najprv neposiela a neúspešné požiadavky sa neopakujú. Ďalší interval opäť zozbiera nové dáta.

Textové limity sa kontrolujú podľa UTF-16 jednotiek používaných .NET a SQL Serverom. Opisné metadáta (názov, doména, verzia inventára) sa podľa potreby skrátia bez rozdelenia Unicode znaku. Surový stav entity sa nikdy neskracuje: ak je neplatného typu, obsahuje neplatný Unicode alebo presahuje DB limit, vynechá sa iba táto entita a report je neúplný. Existujúci stav `null` zostáva dostupný a nemení sa na `Missing`. Chýbajúca entita sa naďalej posiela ako `Missing` s hodnotou `null`.

Dostupnosť entity (`availability`) sa od verzie 0.1.5 vyhodnocuje v tomto poradí: entita nie je v stavoch ani v registri → `Missing` (4); entita je v registri vypnutá (`disabled_by`) → `Disabled` (5); stav `unavailable` alebo zapnutá entita z registra bez stavu → `Unavailable` (2); stav `unknown` → `Unknown` (3); inak `Available` (1). `Unknown` teda znamená iba bežiacu entitu, ktorá ešte nemá hodnotu, napríklad MQTT senzor bez prijatej správy od reštartu HA. Hodnotu 5 prijíma iba backend s podporou `Disabled`; backend preto nasaďte pred aktualizáciou integrácie.

Logy rozlišujú timeout, chybu TLS, sieťové zlyhanie a HTTP odpoveď vrátane stavov 400 (neplatný report), 413 (priveľký report), 429 (limit požiadaviek) a 5xx (chyba servera). Pri neúplnom reporte sa do HA logu zapíše rovnaký bezpečný diagnostický text ako do `errorMessage`. Obsah chybových odpovedí, payload, integračný kľúč ani text neočakávaných výnimiek sa nelogujú. Ani pri 429 sa nezaraďuje opakovanie; čaká sa na ďalší bežný interval.

Na jednu HA inštaláciu je povolená jedna konfigurácia BlackLabs Watchdog vrátane ochrany pred súbežnými sprievodcami. Pri unload/reload sa najprv zruší plánovanie a potom rozpracované odosielanie.

Prenositeľné testy spúšťajte z adresára `Integration` v samostatnom Python prostredí. Používajú skutočné knižnice Voluptuous, aiohttp a JSON Schema; HA rozhrania majú testovacie náhrady. HTTPS testy používajú lokálny server a dočasnú certifikačnú autoritu, bez kontaktovania skutočného backendu. Kontraktový test validuje zozbieraný payload proti OpenAPI, preto pred ním spustite backendový build.

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

Samostatná sada `tests/ha` používa skutočné flow managers, selektory, udalosti, časovače a reload/unload HA. Vyžaduje Linux a Python 3.14.2 alebo novší. Pripnutý testovací plugin používa HA 2026.9.3. Spúšťajte ju v inom prostredí ako prenositeľné testy, aby sa testovacie náhrady HA nemiešali so skutočným frameworkom:

```sh
python3.14 -m venv .venv-ha
. .venv-ha/bin/activate
python -m pip install -r requirements-ha-test.txt
python -m pytest -q tests/ha
```

Lokálne prešlo 54 prenositeľných Python testov vrátane simulácie HA 2026.2.2; predchádzajúce zmeny overili backendový build, testy nad izolovanou SQLite databázou, frontendový typecheck a lint. Sada pre skutočný HA, `hassfest`, HACS runtime a odosielanie na testovacej HA inštalácii zatiaľ neboli spustené; Windows pracovisko nemá Linux/WSL. HA 2026.9.3 je cieľ testov, nie deklarácia overenej prevádzkovej kompatibility. Žiadna verzia HACS zatiaľ nebola prevádzkovo overená.

Štruktúra balíka je určená aj pre HACS. Backendové testy sa z koreňa projektu spúšťajú cez `dotnet test App/Backend.Tests/HomeFleet.Api.Tests.csproj`.
