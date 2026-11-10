# polgate

[![CI](https://github.com/boromat/polgate/actions/workflows/ci.yml/badge.svg)](https://github.com/boromat/polgate/actions/workflows/ci.yml)
[![Silnik maskowania: polanon](https://img.shields.io/pypi/v/polanon?label=silnik%3A%20polanon)](https://pypi.org/project/polanon/)
[![Obraz Dockera](https://img.shields.io/badge/docker-ghcr.io%2Fboromat%2Fpolgate-blue)](https://github.com/boromat/polgate/pkgs/container/polgate)
[![Licencja: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[English](README.md) | **Polski**

**Hub uruchamiany na własnym serwerze, dzięki któremu pracownicy mogą korzystać z Claude'a lub GPT
przy prawdziwych materiałach służbowych, nie wysyłając danych osobowych do dostawcy modelu.** Dane
osobowe są maskowane lokalnie, zanim zapytanie opuści sieć, i przywracane w odpowiedzi. Projekt
powstał z myślą o polskich danych: PESEL, NIP, REGON, numery dowodów osobistych i IBAN są
weryfikowane sumą kontrolną, numery KRS są rozpoznawane po etykiecie, która przy nich stoi, nazwy
firm po formie prawnej, a osoby i miejsca wskazuje polski model NER. Obsługiwane są też faktury
z zagranicy: zagraniczne numery VAT i numery rejestrowe firm, rachunki bankowe, adresy i telefony są
rozpoznawane po etykietach i formatach, w jakich się je drukuje. Dostawca dostaje token w miejsce
wszystkiego, co znajdą reguły i model; to, czego nie znajdą, wychodzi tak, jak zostało wpisane,
a znane luki wymienia [model zagrożeń](docs/THREAT_MODEL.md).

Silnikiem maskowania jest [polanon](https://github.com/boromat/polanon), osobna biblioteka, którą pip
instaluje z [PyPI](https://pypi.org/project/polanon/); to repozytorium zawiera hub zbudowany wokół
niej.

![Użytkownik wysyła wiadomość z imieniem i nazwiskiem, numerem PESEL, numerem telefonu, adresem
e-mail, numerem IBAN i nazwą firmy; odpowiedź spływa strumieniowo z oryginałami na właściwych
miejscach, a „Co zobaczył model” pokazuje, że dostawca dostał tylko tokeny, takie jak OSOBA_1
i PESEL_1.](docs/images/demo.gif)

<sub>Nagrane na komputerze programisty, z atrapą dostawcy i syntetycznymi danymi.</sub>

## Co robi

```
pracownik ──▶ hub (Twój serwer) ──▶ model w chmurze
   "Anna Wiśniewska, PESEL 45831523452, konto PL61109010140000071219812874"
        │  etap 1  deterministyczne detektory: regex + sumy kontrolne + polski NER + Twoja lista
        │          „Zawsze maskuj”
        │  etap 2  opcjonalnie mały lokalny model (Ollama) szuka w zamaskowanym tekście tego,
        │          co wciąż kogoś identyfikuje; domyślny JUDGE_BACKEND=mock to atrapa do pokazu
        ▼
   "<OSOBA_1>, PESEL <PESEL_1>, konto <IBAN_1>"        ← to, co widzi dostawca
        ▼
   odpowiedź wraca strumieniowo z tokenami, a hub w locie wstawia w ich miejsce oryginały
```

## Wypróbuj

**Hub jednym poleceniem.** Niczego nie trzeba wcześniej konfigurować: przy pierwszym starcie hub sam
generuje własne klucze oraz hasło administratora i przechowuje je w wolumenie razem z bazą danych.

```bash
docker run -d --name polgate -p 8000:8000 -v polgate-data:/data ghcr.io/boromat/polgate
until docker exec polgate test -s /data/initial-admin-password; do sleep 1; done  # czeka na koniec pierwszego startu
docker exec polgate cat /data/initial-admin-password
```

Otwórz http://localhost:8000, zaloguj się jako `admin` tym hasłem i zmień je na stronie konta.
Maskowanie, wgrywanie plików, podgląd „Co zobaczył model” i panel administracyjny działają od razu;
żeby dostawać odpowiedzi, dodaj klucz dostawcy opcją `-e LLM_API_KEY=...` (domyślnie OpenRouter,
a dowolny endpoint zgodny z API OpenAI przez `LLM_BASE_URL`). Obraz działa na amd64 i arm64.

**Samo maskowanie we własnym kodzie.** To właśnie polanon, którego rdzeń nie potrzebuje niczego
poza biblioteką standardową:

```bash
pip install polanon
polanon "Jan Kowalski, PESEL 50810112354, konto PL61109010140000071219812874"
# <OSOBA_1>, PESEL <PESEL_1>, konto <IBAN_1>
# mapowanie tokenów na oryginały trafia na stderr albo, z --mapping PATH, do pliku
```

[README biblioteki polanon](https://github.com/boromat/polanon#readme) pokazuje API w Pythonie,
przywracanie w strumieniu, opcjonalny model językowy i sędziego etapu 2.

## Skuteczność wykrywania

Za wykrywanie odpowiada polanon i to w nim jest benchmark, który je mierzy. Na jego jedenastu
anotowanych dokumentach przykładowych (ośmiu polskich i trzech fakturach z zagranicy) same reguły
w pełni maskują 86% wartości, a reguły z `pl_core_news_md` 98%, i żadna wartość nie zostaje
zamaskowana tylko częściowo; Presidio na tych samych dokumentach i z tym samym modelem w pełni
maskuje 66%, a w najłagodniejszym ustawieniu 76%. Jedenaście krótkich dokumentów to test regresji,
a nie benchmark badawczy; napisał je autor polanon, który dostrajał na nich reguły biblioteki.
[Notatki do benchmarku](https://github.com/boromat/polanon/blob/main/benchmarks/README.md) podają
wyniki dla każdego typu danych i opisują, co zostaje pominięte i dlaczego.

To, co dociera do dostawcy, hub mierzy we własnej [ewaluacji wycieków](evals/README.md): 27
zmyślonych rozmów, 60 tur i cztery załączone pliki, przepuszczonych przez działający hub do atrapy
dostawcy, która zapisuje każde żądanie. Z polanon 0.2.2 do dostawcy dotarły 4 ze 125 wartości, które
miały zostać w firmie, 2 z 76 zwykłych zwrotów zostały niepotrzebnie zamaskowane, żadna tura nie
została odrzucona, a każda z 60 odpowiedzi wróciła dokładnie w tej postaci, w jakiej wpisano
wiadomość. Te rozmowy też napisał autor huba, a notatki do ewaluacji opisują każde pominięcie
(po angielsku).

## Funkcje

- **Czat jak w każdym asystencie**: rozmowy, historia i odpowiedzi przesyłane strumieniowo.
- **„Co zobaczył model”** przy każdej wiadomości, żeby nikt nie musiał ślepo ufać maskowaniu.
- **Wgrywanie plików**: PDF, DOCX, XLSX, CSV, JSON, TXT, a gdy zainstalowany jest tesseract, także
  obrazy i zeskanowane PDF-y. Pliki mogą wymagać zatwierdzenia, zanim ich zamaskowana treść zostanie
  dołączona.
- **Stabilne tokeny**: ta sama osoba pozostaje `<OSOBA_1>` tak długo, jak sejf przechowuje
  mapowanie (domyślnie 30 dni), więc model może się do niej odnosić w kolejnych turach rozmowy.
- **Zaszyfrowany sejf** na mapowanie tokenów i **retencja, która naprawdę usuwa**: mapowania,
  historia wiadomości i wgrane pliki znikają zgodnie z harmonogramem ustawionym przez administratora.
- **Dziennik audytu**, który zapisuje typy danych, ich liczby i skróty (hashe), a nigdy same wartości.

  ![Dziennik audytu: wpis o wysłanej wiadomości wymienia rodzaje zamaskowanych danych — EMAIL, IBAN,
  ORG, PERSON, PESEL i PHONE, po jednym z każdego — i nie zawiera żadnej wartości.](docs/images/audit.png)

- **Polityka ustalana przez administratora**: tryb zatwierdzania dla każdego kanału, działanie
  etapu 2, retencja, maskowane typy danych, nazwy, które trzeba maskować zawsze, i nazwy, których nie
  maskuje się nigdy — Facebook, Google Ads, Canva i inne platformy, o których rozmawia zespół,
  rozpoznawane w każdej formie odmiany.
- **Dowolny dostawca zgodny z API OpenAI**: OpenRouter, OpenAI albo w pełni lokalny model w Ollamie.
  Przy pustym `LLM_API_KEY` nagłówek `Authorization` nie jest wysyłany, więc działa też endpoint,
  który nie wymaga klucza.

## Bezpieczeństwo

Hub ma dostęp do odszyfrowanej historii i do mapowania, które odwraca każde maskowanie, dlatego
zabezpieczenia od strony przeglądarki są traktowane równie poważnie jak kontrola tego, co wychodzi
do dostawcy.

- **Żadna znana wartość oryginalna nie wychodzi.** Funkcja `assert_no_raw_values` jest wywoływana
  przed każdym zapytaniem czatu do dostawcy (sędzia etapu 2 w trybie `remote`, narzędzie do testów,
  wysyła tekst po etapie 1 bez tej kontroli) i zgłasza wyjątek, jeśli którakolwiek znana wartość
  oryginalna pojawi się w wysyłanych danych znak w znak, poza tokenami tej rozmowy; krótka liczba,
  np. PIN, jest brana pod uwagę tam, gdzie polanon by ją zamaskował, więc data zawierająca jej cyfry
  już nie. Pilnują tego kod i testy, a nie uprzejma prośba do modelu.
- **Content-Security-Policy bez `unsafe-inline` i bez `unsafe-eval`.** Nigdzie w szablonach nie ma
  skryptu inline, handlera zdarzeń inline ani atrybutu `style=`; logika strony znajduje się w jednym
  pliku w `/static` i podpina się przez atrybuty `data-`. Test nie przejdzie, jeśli coś z tego wróci.
- **CSRF**: ciasteczka sesji `SameSite=Lax` oraz sprawdzanie pochodzenia (origin), które odrzuca
  żądania zmieniające stan wysłane z innej witryny.
- **Logowanie**: argon2id; blokada konta po powtarzających się nieudanych próbach, liczonych na
  podstawie dziennika audytu, odporna na równoległe zgadywanie; sesje, które po zmianie hasła kończą
  się wszędzie; hasło o długości co najmniej 10 znaków; `?next=` ograniczone do ścieżek wewnątrz
  huba.
- **Dane w spoczynku**: Fernet dla sejfu, historii wiadomości i tekstu wyciągniętego z plików; wgrane
  pliki są zapisywane z uprawnieniami `0600`.

Uczciwą wersję, łącznie z tym, przed czym nic z tego nie chroni, zawiera `docs/THREAT_MODEL.md`.

## Silnik i hub

| Część | Gdzie jest | Do czego służy |
|---|---|---|
| [polanon](https://github.com/boromat/polanon) | własne repozytorium, instalacja z [PyPI](https://pypi.org/project/polanon/) | biblioteka do anonimizacji: detektory (guards), sędzia, stabilne tokeny, przywracanie w strumieniu, kontrola odpowiedzi |
| `polgate/` | to repozytorium | hub: interfejs w FastAPI i HTMX, konta, sejf, pliki, audyt, kierowanie zapytań do modeli |

polanon nie jest skopiowany do tego repozytorium. `pyproject.toml` deklaruje `polanon[ner,judge]`,
a pip pobiera go z PyPI razem ze spaCy, jak każdą inną zależność; obraz Dockera instaluje go tak
samo podczas budowania. polanon nigdy nie importuje huba, a `tests/test_layering.py` sprawdza na
zainstalowanej wersji zarówno to, jak i obietnicę, że import polanon nie ładuje niczego poza
biblioteką standardową.

Pominięta wartość, fałszywy alarm albo nowy detektor to zmiana w polanon, więc trafia do
[repozytorium polanon](https://github.com/boromat/polanon) i do huba dociera w kolejnym wydaniu
polanon.

## Uruchomienie ze źródeł

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # pobiera polanon i spaCy z PyPI
.venv/bin/python -m spacy download pl_core_news_md
./run.sh
```

`run.sh` sprawdza środowisko, przy pierwszym użyciu tworzy `.env` ze świeżo wygenerowanymi kluczami
i uruchamia hub pod adresem http://127.0.0.1:8000 z automatycznym przeładowaniem. Jeśli czegoś
brakuje, podaje dokładne polecenie, które trzeba wykonać; nigdy nie instaluje pakietów systemowych
ani nie nadpisuje istniejącego `.env`.

```bash
./run.sh 8080         # inny port
./run.sh --no-reload  # bez śledzenia zmian w plikach
```

Zaloguj się jako `admin` hasłem z `ADMIN_PASSWORD` w `.env`, a gdy ta zmienna jest pusta, hasłem
z pliku `data/initial-admin-password`. Potem zmień je na stronie konta: panel administracyjny będzie
wyświetlał ostrzeżenie, dopóki tego nie zrobisz. Ustaw `LLM_API_KEY`, żeby korzystać z dostawcy,
który wymaga klucza; bez dostawcy nadal możesz wypróbować maskowanie, wgrywanie plików, podglądy
i panel administracyjny.

**Na Windowsie** kliknij dwukrotnie `start.bat` w katalogu repozytorium albo uruchom go
z terminala. Potrzebny jest Python 3.12 lub nowszy z [python.org](https://www.python.org/downloads/);
przy pierwszym uruchomieniu skrypt tworzy `.venv`, instaluje hub i model spaCy oraz zapisuje `.env`
z wygenerowanymi kluczami, co zajmuje kilka minut. Potem uruchamia hub i otwiera go w przeglądarce.
Przyjmuje te same argumenty co `run.sh` (port i `--no-reload`), a do tego `--no-browser`.

```bat
start.bat
start.bat 8080 --no-reload
```

Te same kroki ręcznie, w PowerShellu:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m spacy download pl_core_news_md
copy .env.example .env     # potem ustaw w nim LLM_API_KEY
python -m uvicorn polgate.main:app --reload
```

Gdy środowisko jest aktywne, polecenia podane dalej działają bez przedrostka `.venv/bin/`.

Przy wdrożeniu w firmie `docker compose up -d` buduje hub z tej kopii repozytorium i go uruchamia.
Czyta `.env`, jeśli taki plik istnieje, więc najpierw skopiuj `.env.example` do `.env` i ustaw w nim
`LLM_API_KEY`; klucze pozostawione puste są generowane w wolumenie danych, tak jak przy `docker run`.
`docker compose --profile ollama up -d` dodaje lokalną Ollamę dla sędziego etapu 2: ustaw
`JUDGE_BACKEND=ollama` w `.env` i raz pobierz model poleceniem
`docker compose exec ollama ollama pull qwen2.5:1.5b`. Gdy hub działa za HTTPS, ustaw
`COOKIE_SECURE=true` w `.env`. Kopię klucza sejfu (`VAULT_KEY` albo `vault.key` w wolumenie danych)
trzymaj gdzieś poza serwerem: bez niego zapisanej historii nie da się już odczytać.

### Aktualizacja

W Dockerze pobierz nowy obraz i uruchom kontener ponownie. Przy instalacji ze źródeł wykonaj
`git pull`, a przed ponownym uruchomieniem `.venv/bin/pip install -e ".[dev]"`, żeby zainstalować
zależności, które w międzyczasie doszły albo wymagają teraz nowszej wersji, w tym polanon. Kolumnę,
którą dodał nowszy model danych, hub dokłada przy starcie i wypełnia wartością domyślną, więc
istniejąca baza danych działa dalej. Gdy schemat wymaga decyzji, której skrypt nie powinien
podejmować sam, `run.sh` zatrzymuje się, zamiast uruchamiać hub, i wypisuje, co należy uruchomić.
Typ danych wykrywany przez nowsze wydanie zostaje włączony w istniejącej polityce, chyba że
maskowanie jest wyłączone; dziennik audytu odnotowuje tę zmianę.

```bash
.venv/bin/python -m polgate.schema     # dodaje to, czego wymaga nowszy model danych, i zgłasza resztę
```

### Konfiguracja

| Zmienna | Znaczenie |
|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | dostawca zgodny z API OpenAI; przy pustym kluczu nagłówek `Authorization` nie jest wysyłany |
| `JUDGE_BACKEND` | etap 2: `mock` (domyślny; atrapa do pokazu, która wyłapuje dwie stałe frazy), `ollama` (model lokalny) albo `remote` (dostawca z wiersza wyżej; zamaskowany tekst opuszcza sieć, tylko do testów) |
| `OLLAMA_BASE_URL`, `OLLAMA_JUDGE_MODEL`, `JUDGE_TIMEOUT_SECONDS` | lokalny sędzia |
| `VAULT_KEY` | klucz Fernet; gdy jest pusty, jest generowany w pliku `vault.key` obok bazy danych. Zrób jego kopię zapasową |
| `SECRET_KEY` | podpisuje ciasteczka sesji; gdy jest pusty, jest generowany w pliku `session.key`. Jego zmiana wylogowuje wszystkich |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | pierwszy administrator; gdy hasło jest puste, hub generuje je w pliku `initial-admin-password` |
| `COOKIE_SECURE`, `SESSION_MAX_AGE_DAYS` | zabezpieczenie ciasteczek i czas życia sesji |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES` | ograniczanie prób logowania |
| `PENDING_TURN_TTL_MINUTES` | jak długo wiadomość w kolejce czeka na swój strumień |
| `HISTORY_BUDGET_CHARS`, `ATTACHMENT_BUDGET_CHARS` | ile rozmowy i ile z każdego dołączonego dokumentu trafia ponownie do modelu w każdej turze |
| `SPACY_MODEL`, `DEFAULT_LANGUAGE` | model NER i język interfejsu, który widzi nowy odwiedzający |

Retencję, tryb zatwierdzania, maskowane typy danych oraz nazwy, które zawsze albo nigdy nie mają być
maskowane, ustawia się w panelu administracyjnym, a nie w `.env`, bo to kwestia polityki, a nie
wdrożenia.

## Czym to nie jest

**Dokumenty zagraniczne są obsługiwane na podstawie formatu, a nie języka.** Pierwsze uruchomienie
z prawdziwym dostawcą przepuściło niezamaskowane nazwy firm, adresy, telefony, numery VAT i szwedzkie
rachunki giro z faktur anglojęzycznych, brytyjskich i szwajcarskich, a pięć z tych faktur zostało
odrzuconych w całości. `InternationalGuard` rozpoznaje teraz te wartości po etykietach
i oficjalnych formatach; na tych samych próbkach nic nie zostaje odrzucone, a niezamaskowane
wychodzą już tylko nazwy miast. Nazwa własna w ciągłym tekście angielskim lub niemieckim oraz każdy
format, którego reguły nie znają, nadal zależą od polskiego modelu językowego, który słabo radzi
sobie z tymi językami.

Miejsca oraz nazwy, których detektory nie znają, są wykrywane statystycznie, więc część z nich
umknie — w benchmarku polanon trzy miejsca z jego jedenastu dokumentów zostają pominięte.
Nazwisko zapisane małą literą, bez imienia ani zwrotu grzecznościowego obok, pozostaje w gestii
tego modelu, a on kieruje się wielkimi literami.
Identyfikacja po kontekście („dyrektor finansowy jedynej huty w mieście”) to dokładnie to, do czego
służy etap 2, a mały lokalny model też nie wyłapie wszystkiego. Zanim zaczniesz na tym polegać,
przeczytaj `docs/THREAT_MODEL.md`, a o tym, jak to się ma do pseudonimizacji, mówi
`docs/GDPR_NOTES.md`. Żaden z tych dokumentów nie jest poradą prawną, a samo uruchomienie huba nie
sprawia, że organizacja spełnia jakiekolwiek wymogi.

## Rozwój

```bash
.venv/bin/pytest --cov                                    # pokrycie 98%
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy polgate                                    # tryb strict
.venv/bin/python evals/run.py                             # co dociera do dostawcy
```

Wykrywanie jest rozwijane i mierzone w [polanon](https://github.com/boromat/polanon), więc to
repozytorium nie ma własnego benchmarku; [`evals/`](evals/README.md) mierzy, co hub jako całość
przepuszcza do dostawcy. `CONTRIBUTING.md` opisuje pełne wymagania jakościowe,
`docs/ARCHITECTURE.md` przepływ danych, `CHANGELOG.md` historię zmian, a `CLAUDE.md` zasady,
których asystent AI powinien przestrzegać przy pracy w tym repozytorium.

## Licencja

MIT
