# Synchronizacja kalendarzy Apple/iCloud, Google i SQLite

Hestia synchronizuje wskazane kalendarze po stronie serwera. Telefon nie musi
uruchamiać dodatkowej aplikacji. Moduł jest domyślnie wyłączony. Działa w trybie
proxy i standalone, niezależnie od `HESTIA_PERSIST`: dane kalendarza zawsze trafiają
do SQLite wskazanego przez `HESTIA_DB` (domyślnie `/data/hestia.db`).

Każda jawna grupa ma lokalny kalendarz SQLite i co najmniej jeden zewnętrzny
kalendarz. Nie są wybierane automatycznie żadne prywatne kalendarze. Pierwszy obieg
importuje istniejące wydarzenia wybranych kalendarzy i tworzy ich kopie w grupie.
Najpierw sprawdź konfigurację na wydzielonych kalendarzach testowych.

## Połączenie kont

1. Skopiuj [`examples/calendar-sync.json`](../examples/calendar-sync.json) do
   `data/calendar-sync.json`. Początkowo ustaw `"groups": []`, żeby wykonać samo
   wykrywanie kalendarzy. Nazwy kont i endpointów są trwałymi identyfikatorami.
2. Dodaj dane uwierzytelniające do środowiska procesu. Wzorem pozostałych integracji
   Hestii używamy zmiennych środowiskowych i lokalnego, ignorowanego przez Git `.env`.
   Plik JSON zawiera wyłącznie **nazwy zmiennych**, nigdy wartości sekretów.
3. Uruchom wykrywanie obu kont, przepisz dokładne identyfikatory wybranych kalendarzy
   do grupy i sprawdź uprawnienie `writable`. Współdzielony kalendarz musi być już
   zaakceptowany przez konto żony i widoczny w jego CalDAV.
4. Włącz `HESTIA_CALENDAR_CONFIG=/data/calendar-sync.json` w środowisku serwera,
   a następnie zrestartuj Hestię. Zmiana konfiguracji wymaga restartu.

Dla Apple użyj identyfikatora Apple Account żony i hasła aplikacji, utworzonego na
[account.apple.com](https://account.apple.com/). Hasło aplikacji wymaga włączenia
uwierzytelniania dwuskładnikowego. Hasło główne konta nie jest właściwym sekretem
tej integracji. [Instrukcja Apple dotycząca haseł aplikacji](https://support.apple.com/102654).

```dotenv
HESTIA_CALENDAR_CONFIG=/data/calendar-sync.json
HESTIA_ICLOUD_USER=wife@example.com
HESTIA_ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
HESTIA_GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
HESTIA_GOOGLE_CLIENT_SECRET=your-client-secret
HESTIA_GOOGLE_REFRESH_TOKEN=your-offline-refresh-token
```

Kalendarz „Na moim iPhonie” nie jest kalendarzem iCloud i nie jest dostępny przez
CalDAV. Publiczny link ICS daje tylko odczyt i nie zapewnia tej synchronizacji.
Apple pozwala właścicielowi prywatnie współdzielonego kalendarza nadać edycję albo
sam odczyt; samo zaproszenie nie gwarantuje możliwości zapisu.
[Udostępnianie kalendarza iCloud](https://support.apple.com/guide/icloud/share-a-calendar-mm6b1a9479/icloud).
Hestia odczytuje rzeczywiste uprawnienia CalDAV/Google i pokazuje ograniczenia;
endpoint bez zapisu nie spełnia pełnej synchronizacji i nie jest traktowany jako
źródło usunięć przy błędzie dostępu.

Dla Google utwórz projekt Google Cloud, włącz Google Calendar API i skonfiguruj ekran
zgody OAuth. Do jednorazowego pozyskania refresh tokena można użyć oficjalnego
[OAuth Playground](https://developers.google.com/oauthplayground/): utwórz klienta
OAuth typu **Web application** z dozwolonym przekierowaniem
`https://developers.google.com/oauthplayground`. W ustawieniach Playground włącz
„Use your own OAuth credentials”, wpisz własnego klienta i sekret oraz wybierz
„Access type: Offline” i „Force prompt: Consent Screen”. Bez własnych danych klienta
Playground unieważnia refresh token po 24 godzinach. Autoryzuj własne konto z zakresami
`https://www.googleapis.com/auth/calendar.events` i
`https://www.googleapis.com/auth/calendar.calendarlist.readonly`, wymień kod na
tokeny i zachowaj refresh token w środowisku. Zakresy umożliwiają listowanie
kalendarzy i edycję wydarzeń; konfiguracja Hestii wybiera konkretne kalendarze
do synchronizacji. Nie zapisuj pliku tokenów
w repozytorium. Przy statusie **Testing** zgoda na dostęp do kalendarza i refresh
token wygasają po 7 dniach. Dla stałej instalacji wybierz projekt w Google Cloud
Console, przejdź do **Google Auth Platform → Audience → Publishing status** i kliknij
**Publish app**, aby ustawić **In production**. Prywatny użytek przez mniej niż 100
osób nie wymaga pełnej weryfikacji OAuth; może pozostać ostrzeżenie o niezweryfikowanej
aplikacji. Po zmianie statusu uzyskaj nowy refresh token przez Playground i podmień
`HESTIA_GOOGLE_REFRESH_TOKEN` w `.env`. Następnie uruchom
`docker compose up -d --force-recreate hestia`, ponieważ sam restart nie wczyta
zmienionego środowiska. Status produkcyjny usuwa limit 7 dni trybu Testing;
token nadal może zostać cofnięty przez użytkownika lub wygasnąć z innych przyczyn.
[Google: status publikacji](https://support.google.com/cloud/answer/15549945?hl=en),
[Google: prywatny użytek bez weryfikacji](https://support.google.com/cloud/answer/13464323?hl=en),
[Google: OAuth dla serwera i dostęp offline](https://developers.google.com/identity/protocols/oauth2/web-server#offline),
[Google: wygaśnięcie refresh tokenów](https://developers.google.com/identity/protocols/oauth2#expiration).

Docker Compose czyta `.env` do interpolacji, ale nie przekazuje automatycznie
każdej jego zmiennej do kontenera. W lokalnym `docker-compose.override.yml` dodaj:

```yaml
services:
  hestia:
    environment:
      HESTIA_CALENDAR_CONFIG: /data/calendar-sync.json
      HESTIA_ICLOUD_USER: ${HESTIA_ICLOUD_USER}
      HESTIA_ICLOUD_APP_PASSWORD: ${HESTIA_ICLOUD_APP_PASSWORD}
      HESTIA_GOOGLE_CLIENT_ID: ${HESTIA_GOOGLE_CLIENT_ID}
      HESTIA_GOOGLE_CLIENT_SECRET: ${HESTIA_GOOGLE_CLIENT_SECRET}
      HESTIA_GOOGLE_REFRESH_TOKEN: ${HESTIA_GOOGLE_REFRESH_TOKEN}
```

Istniejący mount `./data:/data` obejmuje plik konfiguracyjny i bazę danych.
Dla uruchomienia bez Dockera wyeksportuj zmienne w powłoce lub konfiguracji usługi;
Hestia nie wczytuje samodzielnie `.env`. Ogranicz dostęp do `.env`, konfiguracji
usługi i kopii bazy (np. `chmod 600 .env data/calendar-sync.json`).

Po przygotowaniu konfiguracji i środowiska przebuduj kontener:

```sh
docker compose up -d --build
```

Przy uruchamianiu bez Dockera zainstaluj `requirements.txt`, wykonaj `npm ci`
i `npm run build` w katalogu `ui`, ustaw `HESTIA_DB` na trwały plik SQLite
(np. `data/hestia.db`) oraz `HESTIA_CALENDAR_CONFIG` na przygotowany plik JSON
i uruchom `python -m hestia`. Migracje `0004` i `0005` są wykonywane automatycznie
przez istniejący mechanizm Alembica; nie wymagają ręcznych poleceń SQL.
Migracja `0005` dodaje trwałe klucze ponowień tworzenia wydarzeń oraz indeksy
używane przy uzgadnianiu kopii i kolejki.

## Wykrywanie i obsługa

Przykłady dla uruchomionego kontenera:

```sh
docker compose exec hestia python -m hestia.calendar_sync discover --account wife
docker compose exec hestia python -m hestia.calendar_sync discover --account me
docker compose exec hestia python -m hestia.calendar_sync once
docker compose exec hestia python -m hestia.calendar_sync status
docker compose exec hestia python -m hestia.calendar_sync events family
docker compose exec hestia python -m hestia.calendar_sync events family --include-deleted
```

Można też używać `python -m hestia.calendar_sync --config data/calendar-sync.json
--db data/hestia.db <polecenie>`. Dokładna CalDAV `calendar_id` to adres kolekcji
zakończony `/`, zwrócony przez discovery, także dla współdzielonego kalendarza;
nie należy zgadywać adresu serwera `pNN`. Dla Google użyj pełnego ID z discovery,
np. adresu konta albo `…@group.calendar.google.com`, zamiast aliasu `primary`.
Discovery nie dopisuje kalendarzy do grup.
Jeden kalendarz i jeden endpoint mogą należeć tylko do jednej grupy. Nie zmieniaj
identyfikatora endpointu w celu przeniesienia jego mapowań do innej grupy: Hestia
odrzuca ponowne użycie zapisanego identyfikatora z innym kontem, kalendarzem lub
grupą. Usunięty z konfiguracji endpoint jest wyłączany, a jego historia pozostaje.

Lokalny format wydarzenia (`event.json`):

```json
{
  "summary": "Rodzinny spacer",
  "description": "Spotkanie przy parku",
  "location": "Warszawa",
  "start": {"dateTime": "2026-10-24T10:00:00+02:00", "timeZone": "Europe/Warsaw"},
  "end": {"dateTime": "2026-10-24T11:00:00+02:00", "timeZone": "Europe/Warsaw"}
}
```

```sh
python -m hestia.calendar_sync put family --file event.json
python -m hestia.calendar_sync put family --file event.json --event LOCAL_ID --expected-revision 1
python -m hestia.calendar_sync delete family LOCAL_ID --expected-revision 2
```

CLI zwraca identyfikator i rewizję potrzebne do kolejnej operacji. Edycja ze starą
rewizją jest odrzucana. Lokalny zapis działa bez sieci i trafia do trwałej kolejki;
kolejny obieg przeniesie go na podłączone kalendarze. Dwa procesy nie wykonują
jednocześnie obiegu na tej samej bazie: wspólna blokada plikowa chroni CLI i serwer.
Polecenie uruchomione podczas zajętej blokady kończy się komunikatem do ponowienia.
Nie uruchamiaj synchronizacji z różnych hostów na kopiach tej samej bazy.

Panel WWW udostępnia widok kalendarza z wyborem grupy i statusem synchronizacji.
Użytkownicy `admin` i `operator` mogą dodawać, edytować i usuwać wydarzenia;
`viewer` ma odczyt. Uprawnienia egzekwuje również API.
Widok został napisany w React i działa na telefonie oraz komputerze. Pokazuje
tygodniową agendę; serie cykliczne są wyświetlane oddzielnie i edytowane jako całość.
Nie rozwija pojedynczych wystąpień serii do kolumn tygodnia. Formularz zachowuje
istniejące wyjątki, a wyłączenie cykliczności usuwa je razem z regułami serii.
Zmiana typu całodniowe/godzinowe jest dostępna dla zwykłych wydarzeń. Dla serii
z regułami lub wyjątkami formularz pokazuje ograniczenie i blokuje tę zmianę,
aby nie pozostawić dat wyjątków i reguł w niezgodnym typie. Samą treść, godziny
i reguły serii nadal można edytować.
W czasie zapisu pola bieżącego formularza są nieaktywne. Można przejść do innego
wydarzenia; zakończenie poprzedniego zapisu nie usuwa nowego szkicu.
Po konflikcie rewizji lista jest odświeżana, a szkic pozostaje otwarty.
Anulowanie i ponowne otwarcie wydarzenia używa aktualnej rewizji.

Przy niejednoznacznej godzinie zmiany czasu można podać jawne przesunięcie UTC
w opcjach zaawansowanych.

Klient HTTP tworzący wydarzenie przez `POST /api/calendars/events` musi przesłać
`idempotency_key` obok `group` i `payload`. Jest to niezmienny klucz danej próby
utworzenia, np. UUID; dozwolone jest 16–128 znaków `A–Z`, `a–z`, `0–9`, `_`, `-`.
Formularz React tworzy go raz dla nowego szkicu i ponawia zapis z tym samym kluczem.
SQLite zachowuje klucz również po restarcie i usunięciu wydarzenia. Powtórzenie
tej samej znormalizowanej treści zwraca istniejące wydarzenie w jego bieżącym stanie,
bez dodatkowej kopii, nadpisania późniejszych zmian ani cofnięcia usunięcia.
Ponowne użycie klucza z inną treścią zwraca `409`: odszukaj utworzone wydarzenie
na odświeżonej liście i edytuj je z aktualną rewizją. Zwykłe edycje i usunięcia
używają `id` i `revision`, bez `idempotency_key`.
CLI tworzy nowe wydarzenie przy każdym osobnym wywołaniu `put` bez `--event`.

## Reguły i odporność

Lokalne identyfikatory, UID, identyfikatory kopii, ETagi, kursory synchronizacji,
rewizje i kolejka operacji są zapisywane w SQLite. Tombstone usunięcia pozostaje
trwale w bazie; stara kopia nie odtwarza skasowanego wydarzenia. Operacje są
idempotentne, a ponowienia używają tej samej tożsamości kopii po awarii lub restarcie.
Własne echo nie tworzy nowego wydarzenia. Nie scala się wydarzeń wyłącznie po tytule
i godzinie: dwie niezależnie utworzone pozycje mogą być zamierzonymi wydarzeniami.
Worker uzgadnia zmiany z poprawnie pobranych snapshotów, korzystając z odczytów
zbiorczych. Pusta odpowiedź przyrostowa nie skanuje całej historii wydarzeń;
trwała kolejka jest nadal obsługiwana niezależnie.

Konflikt edycji i usunięcia rozstrzyga **usunięcie**, niezależnie od daty edycji.
Nie można przywrócić wydarzenia z tym samym UID; ponowne utworzenie tworzy nowy UID.
Przy równoczesnych edycjach wygrywa ostatni czas modyfikacji dostawcy, a przy równych
czasach — leksykograficznie większy trwały identyfikator endpointu (`local` oznacza
edycję w Hestii). Gdy dostawca nie podaje czasu modyfikacji, używany jest czas
wykrycia zmiany w Hestii. Kolejna rzeczywista zmiana z tego samego zwycięskiego
źródła jest przyjmowana także w tej samej sekundzie znacznika czasu. Zegar serwera
powinien być synchronizowany przez NTP. Silnik
zachowuje bieżącą wersję, mapowania i trwałe usunięcia; nie jest archiwum wszystkich
poprzednich wersji treści. Stan kolejek można sprawdzić w `status`.

Po niepewnym wyniku zapisu (np. zerwanym połączeniu po części żądań zmieniających
serię) Hestia kończy zapisaną operację jako całość, zanim potraktuje zmienioną treść
tej docelowej kopii jako nowe źródło. Edycja tej kopii podczas takiego ponowienia
może zostać zastąpiona oczekującą wersją. Zmiany z pozostałych kalendarzy nadal
podlegają regule czasu modyfikacji, a usunięcie zawsze ma pierwszeństwo. Status
pokazuje nieudane operacje i ich ponowienia; nie ma transakcji obejmującej chmury
Apple, Google i SQLite jednocześnie.
Błędy sieci, HTTP 401/403, brak uprawnień, niepełna odpowiedź lub błąd parsowania
nie oznaczają usunięcia. Google używa przyrostowego `syncToken`, pobiera wszystkie
strony przed zapisaniem kursora, a HTTP 410 wymusza pełny odczyt.
[Google: synchronizacja przyrostowa](https://developers.google.com/workspace/calendar/api/guides/sync).

Daty całodniowe używają `start.date` i `end.date`, gdzie koniec jest **wyłączny**:
jednodniowe wydarzenie 24 października ma koniec `2026-10-25`. Wydarzenia godzinowe
zachowują strefę IANA, np. `Europe/Warsaw`, i przesunięcie czasu; cykliczne wydarzenia
zachowują `RRULE`, `EXDATE`, `RDATE` oraz wyjątki i odwołane wystąpienia. Silnik nie
rozwija serii do nieskończonej listy spotkań.
[Google: model wydarzenia](https://developers.google.com/workspace/calendar/api/v3/reference/events).

Obsługiwane są tytuł, opis, miejsce, czas, całodniowość, podstawowy status,
widoczność i przejrzystość oraz serie i wyjątki. Wydarzenie godzinowe może mieć
zerowy czas trwania; w iCalendar zapisuje się je bez `DTEND`. Pole
`endTimeUnspecified: true` oznacza nieznaną godzinę zakończenia, a nie sztuczny
czas trwania. Integracja zachowuje tę informację przez własny znacznik CalDAV
i prywatne metadane Google. Dla zgodności z Apple/iCloud adapter może zapisać jawny
`DTEND` równy kanonicznemu końcowi, także równy początkowi wydarzenia godzinowego.
Nie dodaje przy tym fikcyjnego dodatniego czasu trwania. Jeśli urządzenie nada
wydarzeniu rzeczywisty koniec, wcześniejszy znacznik nieznanego końca przestaje
obowiązywać. W wydarzeniach niecyklicznych starsze strefy
`GMT+01:00` itp. są normalizowane do UTC z zachowaniem rzeczywistej chwili
wydarzenia. Serie z takim starszym oznaczeniem wymagają równoważnej strefy IANA.

Google potrafi usuwać z opisu importowane stopki konferencji Meet zaczynające się
od `-::~:~`. Hestia zapisuje takie opisy jako tekst z encjami HTML i krótkim
prywatnym znacznikiem zawierającym skrót zapisu. Odczyt odtwarza oryginał tylko
wtedy, gdy skrót nadal pasuje; późniejsza edycja opisu przez użytkownika nie jest
dekodowana na podstawie starego znacznika. Zwykłe opisy pozostają bez tego
przekształcenia. Kontrola po zapisie musi potwierdzić pełną treść.

Importowane są też zwykłe dane terminów utworzonych z Gmaila i urodzin. Google
ogranicza ich późniejszą edycję: we wpisach z Gmaila tytuł, miejsce i czas są
niezmienne przez API; urodziny mają własny ograniczony zestaw modyfikowalnych pól.
Powiązanie urodzin z Kontaktami może dodatkowo zablokować zmianę daty. Hestia nie
zastępuje tych wpisów zwykłymi wydarzeniami Google; niedozwolona zmiana pozostaje
w kolejce z widocznym błędem `google_event_type_restricted`.

Zaproszenia uczestników, alarmy,
załączniki, kolory i konferencje nie są synchronizowane; kopia nie jest mechanizmem
wysyłania zaproszeń. Niestandardowe strefy spoza IANA, czasy „floating” bez strefy,
subsekundy, `RANGE=THISANDFUTURE`, okresy w `RDATE`, `EXRULE` i pozostałe specjalne
typy Google (np. czas skupienia i praca z wybranego miejsca) są odrzucane zamiast
cichej utraty semantyki. Współdzielony kalendarz bez zapisu,
cofnięte hasło aplikacji albo wycofana zgoda OAuth wymagają zmiany uprawnień lub
ponownej autoryzacji. Stan błędu nie usuwa lokalnej historii ani oczekujących zmian.

## Jawne przypisanie strefy czasom „floating”

W iCalendar godzina bez `TZID` i bez końcowego `Z` jest czasem „floating”: oznacza
tę samą godzinę lokalną niezależnie od strefy, w której znajduje się odbiorca.
Google Calendar wymaga określonej strefy lub offsetu, więc takiego znaczenia nie
da się przenieść do Google bez wyboru sposobu przeliczenia.
[RFC 5545 §3.3.5](https://datatracker.ietf.org/doc/html/rfc5545#section-3.3.5),
[Google: początek i koniec wydarzenia](https://developers.google.com/workspace/calendar/api/v3/reference/events).

Domyślnie Hestia odrzuca takie godziny. Po świadomym wyborze można dodać
`"floating_time_zone": "Europe/Warsaw"` do konfiguracji konkretnego kalendarza
CalDAV w grupie. Jest to jawna decyzja o **przypisaniu** strefy: lokalna godzina
otrzymuje offset właściwy dla tej strefy w dniu wydarzenia. Lokalna kopia, Google
oraz późniejsze zwykłe zapisy do CalDAV zawierają już konkretną strefę IANA.
Podczas podróży Google pokaże tę samą chwilę w strefie urządzenia; nie zachowa
reguły „ta sama godzina lokalna w każdej strefie”.

Hestia nie wybiera tej opcji automatycznie na podstawie strefy hosta,
`X-WR-TIMEZONE` ani obecności `VTIMEZONE`. Właściwość kolekcji CalDAV
`calendar-timezone` może pomóc podjąć decyzję, lecz służy serwerowi do przeliczania
raportów i sama nie zmienia znaczenia zapisanych czasów floating.
[RFC 4791 §5.2.2](https://datatracker.ietf.org/doc/html/rfc4791#section-5.2.2).

Opcja dotyczy wyłącznie godzin bez własnej strefy: nie zmienia dat całodniowych,
UTC ani godzin z jawnym `TZID`. Obejmuje też granicę cyklu `UNTIL`, dodatkowe
daty, wykluczenia i wyjątki. Niejednoznaczna lub nieistniejąca godzina podczas
zmiany czasu jest nadal odrzucana; wymaga wskazania jednoznacznej chwili w źródle.
Bez świadomie wybranej polityki należy poprawić strefę wydarzenia w kalendarzu
źródłowym albo pozostawić taki kalendarz poza synchronizacją.

## Testy

```sh
python -m unittest discover -s tests -p 'test_calendar*.py'
```

Testy używają atrap dostawców i tymczasowych baz SQLite; nie dotykają prywatnych
kalendarzy. Przed włączeniem własnej grupy zweryfikuj dodanie, edycję, usunięcie,
serię z wyjątkiem i zmianę czasu na wydzielonych kalendarzach iCloud i Google.
