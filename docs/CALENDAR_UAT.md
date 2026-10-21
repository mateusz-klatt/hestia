# UAT formularza kalendarza

Weryfikacja 22 października 2026: **15/15 scenariuszy PASS** w Google Chrome
headless, sterowanym przez Playwright. Testowano rzeczywisty komponent React,
z atrapami wszystkich odpowiedzi API i fikcyjnymi wydarzeniami. Test nie wykonywał
żądań do produkcyjnej Hestii, Google ani iCloud.

## Zachowanie formularza

Nowe wydarzenie godzinowe trwa domyślnie godzinę. Zmiana początku przesuwa koniec
z zachowaniem bieżącej długości; ręczna zmiana końca ustala długość kolejnego
przesunięcia. Niepełny lub niepoprawny zapis daty nie przesuwa końca. Błąd zapisu
pozostawia formularz i szkic do poprawienia lub ponowienia.

Formularz zachowuje **czas według zegara w wybranej strefie**: przeniesienie
09:00–10:00 na inny dzień nadal daje 09:00–10:00. Przedział przechodzący przez
zmianę czasu może mieć inną długość w sekundach. Serwer nadal sprawdza strefę IANA,
nieistniejące godziny wiosenne i dwuznaczne godziny jesienne. Zmienione pola tracą
stare przesunięcie UTC; nietknięte znaczniki czasu zachowują swoje przesunięcia.
Ręcznie wpisane przesunięcie musi odtwarzać tę samą godzinę w wybranej strefie;
formularz odrzuca przesunięcia niezgodne ze strefą i nieistniejące godziny wiosenne.
Nieznany koniec nie podlega tej kontroli.

Podczas zapisu można otworzyć inne wydarzenie lub grupę. Zakończenie wcześniejszego
zapisu nie zamyka nowego formularza, nie usuwa jego szkicu i nie odświeża starej grupy.
Pola bieżącego formularza są na czas zapisu nieaktywne. Odpowiedź `409` odświeża
listę, zachowując szkic; anulowanie i ponowne otwarcie używa aktualnej rewizji.

Zmiana zwykłego wydarzenia między całodniowym a godzinowym zachowuje zajęte dni.
Jeden dzień przechodzi na `09:00–10:00` tego samego dnia; w wydarzeniu wielodniowym
koniec przypada na ostatni zajęty dzień. Końcowa północ pozostaje wyłączną granicą.
Zmiana tego typu dla serii z regułami lub wyjątkami jest zablokowana z wyjaśnieniem.
Usunięcie cykliczności, zapis i ponowne otwarcie umożliwiają zmianę typu.

Nowy szkic ma stały klucz ponowienia tworzenia. Po utracie odpowiedzi zapis jest
ponawiany z tym samym kluczem, aby API zwróciło istniejące wydarzenie bez duplikatu.
Klucz jest przechowywany w otwartym szkicu; przeładowanie strony nie zachowuje szkicu.

Inspiracją jest ograniczenie liczby ręcznych poprawek przy zachowaniu kontroli
nad długością wydarzenia. Google dokumentuje możliwość ustawienia domyślnej
długości i zmiany jej przez użytkownika
([Google Workspace](https://knowledge.workspace.google.com/admin/calendar/set-the-default-duration-for-events-in-your-organization)).
Outlook pozwala skracać spotkania przez późniejszy początek lub wcześniejszy koniec
([Microsoft Support](https://support.microsoft.com/en-us/outlook/calendar/end-meetings-early-or-start-late)).
Automatyczne przesuwanie końca i godzinny domyślny czas są decyzjami Hestii;
przytoczone instrukcje nie gwarantują takiego samego sprzężenia pól w tych produktach.

## Sprawdzone scenariusze i kroki powtórzenia

| # | Scenariusz | Kroki i oczekiwany wynik | Wynik |
| --- | --- | --- | --- |
| 1 | Nowe wydarzenie | Po „Dodaj wydarzenie” fokus trafia do tytułu. Początek `2026-10-24 23:30` ustawia koniec `2026-10-25 00:30`. Ręcznie ustaw `01:00`, następnie początek `2026-11-02 09:00`: koniec ma wynosić `10:30`. Zapis wysyła te godziny. | PASS |
| 2 | Edycja i błędy zapisu | Otwórz wydarzenie z jawnym przesunięciem UTC, zmień sam tytuł: przesunięcia pozostają. Wymuś kolejno odpowiedzi POST `409` i `503`: komunikat jest widoczny, szkic pozostaje. Przywróć `200` i ponów zapis. | PASS |
| 3 | Usunięcie | Anuluj potwierdzenie: brak POST usunięcia. Potwierdź ponownie: wysyłane jest żądanie usunięcia i pojawia się informacja o lokalnym zapisie. | PASS |
| 4 | Wydarzenie całodniowe | Początek `2026-12-31` daje wyłączny koniec `2027-01-01`. Zmień koniec na `2027-01-03`, potem początek na `2027-02-01`: wyłączny koniec ma wynosić `2027-02-04`, zachowując trzy dni. | PASS |
| 5 | Nieznany koniec | Zaznacz „Nieznany czas zakończenia”: pole końca jest nieaktywne. Zmień początek i zapisz: payload ma `endTimeUnspecified: true`, a techniczny koniec jest równy początkowi wydarzenia godzinowego. | PASS |
| 6 | Widok mobilny, Warszawa | Przy rozmiarze `390×844` i strefie przeglądarki `Europe/Warsaw` wprowadź początek `2026-10-26 09:00`: koniec wynosi `10:00`, strona nie przewija się poziomo i nie zgłasza błędów JavaScript. | PASS |
| 7 | Widok mobilny, Los Angeles | Powtórz scenariusz 6 przy strefie przeglądarki `America/Los_Angeles`. Długość i brak poziomego przewijania pozostają takie same. | PASS |
| 8 | Uprawnienia viewer | Otwórz szczegóły jako viewer: brak przycisku dodawania, formularza edycji i przycisku zapisu; nie są wysyłane żądania zapisu. | PASS |
| 9 | Jawne przesunięcie i zmiana czasu | Wpisz początek `2026-03-29 02:30`, strefę `Europe/Warsaw` i przesunięcie `+01:00`: formularz pokazuje błąd bez POST i zachowuje szkic. Popraw początek na `03:30`, przesunięcie na `+02:00`: zapis przechodzi. | PASS |
| 10 | Zmiana edytora podczas zapisu | Opóźnij POST wydarzenia A. Otwórz formularz B i wpisz tytuł. Zakończ POST A: formularz B pozostaje otwarty z niezmienionym szkicem. | PASS |
| 11 | Całodniowe i godzinowe | Wpisz `2026-12-31 09:00–10:00`, zaznacz i odznacz „Cały dzień”: wraca ten sam dzień `09:00–10:00`. Ustaw koniec `2027-01-02 10:00`: całodniowy koniec wyłączny wynosi `2027-01-03`; powrót do godzinowego daje `2027-01-02 10:00`. | PASS |
| 12 | Aktualna rewizja po konflikcie | Podczas edycji A zmień wersję na serwerze. POST zwraca `409`: szkic zostaje, lista otrzymuje nową wersję. Anuluj i otwórz wydarzenie ponownie: kolejny zapis wysyła aktualną rewizję. | PASS |
| 13 | Utrata odpowiedzi tworzenia | Atrapa API zapisuje nowe wydarzenie, po czym zrywa odpowiedź. Ponów zapis: oba POST mają ten sam `idempotency_key`, a API zachowuje jedno nowe wydarzenie. | PASS |
| 14 | Pola podczas zapisu | Opóźnij POST bieżącego formularza: wszystkie jego pola i przyciski są nieaktywne. Dodawanie innego wydarzenia i nawigacja pozostają dostępne. Zakończ POST: pojawia się sukces. | PASS |
| 15 | Typ dat w serii | Otwórz serię z RRULE: przełącznik „Cały dzień” jest nieaktywny, a widoczne wyjaśnienie opisuje ograniczenie. Brak żądań zapisu. | PASS |

Do ręcznego powtarzania podstawowych scenariuszy uruchom atrapy interfejsu:

```sh
cd ui
npm ci
npm run dev -- --host 127.0.0.1 --port 5179
```

Otwórz `http://127.0.0.1:5179/?mock` i zakładkę kalendarza. Parametr `?mock`
działa w serwerze developerskim; dane są fikcyjne i znikają po przeładowaniu.
Przed edycją wybierz tydzień zawierający wydarzenie testowe. W DevTools włącz
widok urządzenia `390×844`; strefę zmień w panelu Sensors → Location → Timezone.
Wykonuj scenariusze, obserwując również żądania w panelu Network. Prosta atrapa
`?mock` nie odtwarza trwałej idempotencji SQLite; scenariusz 13 wykonano z osobną
atrapą API zapamiętującą klucze i z zerwaniem odpowiedzi po zapisie. Trwałość po
restarcie, późniejszej edycji i usunięciu sprawdzają testy
`tests/test_calendar_idempotency.py` na tymczasowych bazach SQLite.

Do scenariuszy 2 i 10 można w konsoli tej **lokalnej strony z `?mock`** przechwycić POST:

```js
(() => {
  const originalFetch = window.fetch.bind(window);
  window.calendarUatStatus = 200;
  window.calendarUatDelay = false;
  window.calendarUatResume = null;
  window.fetch = async (input, init) => {
    const url = String(input instanceof Request ? input.url : input);
    if (init?.method === "POST" && url.includes("/api/calendars/events")) {
      if (window.calendarUatDelay) {
        await new Promise(resolve => { window.calendarUatResume = resolve; });
      }
      if (window.calendarUatStatus !== 200) {
        return new Response("{}", { status: window.calendarUatStatus });
      }
    }
    return originalFetch(input, init);
  };
})();
```

Ustaw `window.calendarUatStatus = 409`, następnie `503`, a na końcu `200` przed
kolejnymi próbami zapisu. Do scenariusza 10 ustaw `window.calendarUatDelay = true`,
zapisz A i otwórz szkic B. Następnie ustaw `window.calendarUatDelay = false` i wykonaj
`window.calendarUatResume?.()`: szkic B ma pozostać otwarty.
Do scenariusza 8 można zamontować ten sam komponent
bez uprawnienia zapisu, nadal na stronie z atrapami:

```js
const { mountCalendar } = await import("/src/calendar.tsx");
const viewer = document.createElement("main");
document.body.replaceChildren(viewer);
mountCalendar(viewer, false).setActive(true);
```

Przeładowanie strony usuwa modyfikacje konsoli. Kontrolę ról po stronie serwera
sprawdzają osobne testy API; powyższa próba viewer weryfikuje interfejs.

## Testy automatyczne i ograniczenia

Z katalogu `ui`:

```sh
npm test -- --maxWorkers=2 --testTimeout=15000
TZ=America/Los_Angeles npm test -- src/calendar.test.tsx --maxWorkers=2 --testTimeout=15000
npm run typecheck
npm run lint
npm run build
```

W tej weryfikacji przeszło **577 testów w 24 plikach**, w tym 55 testów kalendarza
także przy strefie środowiska Los Angeles. Testy komponentu obejmują dodatkowo
niepełny początek, ręczny czas 30 minut, zerową długość, zmianę czasu i zachowanie
niezmienionych przesunięć, poprawne jesienne przesunięcia oraz opóźnione odpowiedzi
po zmianie grupy lub ponownym otwarciu tego samego wydarzenia.
Obejmują również konwersję dat w obu kierunkach, aktualną rewizję po `409`,
ochronę pól podczas zapisu i stałą tożsamość ponowienia nowego szkicu.
Sprawdzenie typów, lint i build również przeszły.

Widok mobilny był emulacją Chrome. **Nie testowano Safari, fizycznego iPhone’a ani
Androida, natywnej klawiatury telefonu ani pełnej obsługi klawiaturą/czytnikiem ekranu.**
Ten UAT interfejsu nie potwierdza synchronizacji z dostawcami — jej testy opisuje
[instrukcja synchronizacji](CALENDAR_SYNC.md).

Wydarzenia całodniowe nadal pokazują techniczną, wyłączną datę końca. Edycja
cykliczności wymaga reguł RRULE/EXDATE/RDATE; nie ma jeszcze prostego kreatora,
a widok pokazuje serie bez rozwijania poszczególnych wystąpień.
