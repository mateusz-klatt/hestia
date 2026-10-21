# Dostawcy kalendarzy: zweryfikowane możliwości i kontrakt

Stan dokumentacji sprawdzony 21 października 2026. Instrukcja uruchomienia i konfiguracji znajduje się w [CALENDAR_SYNC.md](CALENDAR_SYNC.md).

## Google Calendar

Hestia używa Google Calendar API v3 z OAuth 2.0 i odnawialnym tokenem. Dostęp do wydarzeń wymaga zakresu `https://www.googleapis.com/auth/calendar.events`, a wykrywanie kalendarzy i sprawdzanie ich uprawnień zakresu `https://www.googleapis.com/auth/calendar.calendarlist.readonly`. Sam zakres `calendar.events.owned` nie wystarcza do cudzych kalendarzy współdzielonych. [Autoryzacja](https://developers.google.com/workspace/calendar/api/auth).

`calendarList` ujawnia faktyczną rolę konta dla danego kalendarza. Hestia dopuszcza `writer` i `owner`. `reader`, `freeBusyReader` i `writerWithoutPrivateAccess` nie zapewniają pełnej kopii treści; ostatnia z tych ról umożliwia zapis, lecz ukrywa treść prywatnych wydarzeń. Taki kalendarz jest pokazywany jako niedostępny do pełnej synchronizacji. [Role dostępu](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList).

Adapter odbiera wszystkie strony `events.list`, `showDeleted=true`, `singleEvents=false`, `showHiddenInvitations=true`, bez ograniczeń dat. Zapisuje `nextSyncToken` dopiero z końca kompletnego odczytu. Błąd HTTP 410 uruchamia pełny odczyt; lokalne mapowania, kolejka i zapisy usunięć pozostają zachowane. Aktualizacje serii i wyjątków uruchamiają pełną rekonstrukcję ich treści. [Synchronizacja przyrostowa](https://developers.google.com/workspace/calendar/api/guides/sync), [lista wydarzeń](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).

Identyfikator nowej kopii jest deterministyczny i zgodny z alfabetem base32hex. Logiczny UID Hestii jest zapisany jako prywatna właściwość `hestia_uid`; dla istniejących wydarzeń odczytywany jest `iCalUID`. Google nie pozwala jednocześnie podać własnego `id` i `iCalUID` przy `insert`, dlatego UID wyświetlany przez sam Google może różnić się od UID pierwotnego wydarzenia. [Tworzenie wydarzeń](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert).

Google czasem zwraca widoczne pojedyncze wystąpienie po zniknięciu jego serii. Hestia sprawdza wtedy rodzica przez `events.get`. Dopiero potwierdzone 404/410 lub stan `cancelled` pozwala zachować wystąpienie jako osobne wydarzenie bez wymyślania reguły cyklu. Otrzymuje ono stabilny własny UID i mapowanie do pierwotnego identyfikatora wystąpienia; jego edycja lub usunięcie dotyczy wyłącznie tej kopii. Błąd sieci, brak uprawnień lub odnalezienie aktywnego rodzica pominiętego w pełnym odczycie zatrzymuje odczyt. Metadane odziedziczone z serii nie łączą takiego wydarzenia z UID rodzica. Wersjonowanie kursora wymusza pełny odczyt po zmianie parametrów zapytania, zachowując lokalne mapowania i usunięcia.

Modyfikacje i usunięcia używają `If-Match`. Konflikt 412 wymaga ponownego odczytu, a ponowienie utworzenia rozpoznaje już istniejący stabilny identyfikator. Serie są przechowywane jako reguły z wyjątkami, bez rozwijania w skończoną listę dat. Zmiana wyjątków wymaga kilku żądań; brak transakcji między nimi jest obsługiwany trwałą kolejką Hestii. [Wersjonowanie](https://developers.google.com/workspace/calendar/api/guides/version-resources), [serie i wyjątki](https://developers.google.com/workspace/calendar/api/guides/recurringevents), [wyszukiwanie wystąpienia przez originalStart](https://developers.google.com/workspace/calendar/api/v3/reference/events/instances).

Wydarzenia `fromGmail` i `birthday` są odczytywane i kopiowane z ich przenośną treścią jako zwykłe wydarzenia w pozostałych kalendarzach. Oryginały podlegają ograniczeniom Google: w `fromGmail` Hestia może zmienić spośród pól treści jedynie widoczność i status; nie może zmienić tytułu, miejsca ani godzin. W `birthday` może zmienić tytuł i jednodniową datę, ale zmiana daty jest zablokowana dla urodzin powiązanych z kontaktem lub profilem właściciela. Nie zmienia typu istniejącego wydarzenia. Nieobsługiwana modyfikacja jest zatrzymywana przed zapisem i pozostaje w kolejce z błędem `google_event_type_restricted`; pozostałe kalendarze nadal mogą się synchronizować. [Ograniczenia typów wydarzeń](https://developers.google.com/workspace/calendar/api/guides/event-types), [ograniczenia aktualizacji z Gmaila](https://developers.google.com/workspace/calendar/api/releases).

## Apple/iCloud i CalDAV

Adapter łączy się z `https://caldav.icloud.com` przez uwierzytelniony CalDAV, wykrywa `current-user-principal`, `calendar-home-set` i konkretne kolekcje kalendarzy. Przekierowania z hasłem dopuszcza wyłącznie w tym samym serwerze lub pomiędzy serwerami HTTPS domeny `.icloud.com`. Kolekcja wybrana do synchronizacji musi mieć jawnie skonfigurowany pełny URL. Zmiana URL wymaga ponownego wykrycia i aktualizacji konfiguracji.

Do konta Apple należy wygenerować osobne hasło aplikacji; wymaga to uwierzytelniania dwuskładnikowego. Odwołanie tego hasła lub zmiana głównego hasła Apple zatrzyma integrację do czasu podania nowego. Obsługiwane aplikacje mogą używać autoryzacji kontem Apple, lecz Hestia nie implementuje tej odrębnej ścieżki. [Oficjalna instrukcja Apple](https://support.apple.com/en-us/102654).

Prywatny kalendarz współdzielony trzeba wcześniej przyjąć na koncie żony. Właściciel musi włączyć „Allow Editing” / „Pozwalaj edytować”. Widoczność kalendarza w aplikacji Apple nie dowodzi prawa zapisu: Hestia sprawdza `DAV:current-user-privilege-set`, odczyt, tworzenie, zmianę oraz usuwanie zasobów VEVENT. Brak tego zestawu uprawnień blokuje synchronizację danego kalendarza. Nie wykonuje próbnych zapisów w prywatnych kalendarzach. [Udostępnianie iCloud na iPhonie](https://support.apple.com/guide/iphone/iph7613c4fb/ios), [aktualne uprawnienia WebDAV, RFC 3744 §5.4](https://www.rfc-editor.org/rfc/rfc3744.html#section-5.4).

Apple nie publikuje pełnego publicznego kontraktu API dla każdej odmiany kalendarza rodzinnego/współdzielonego. Dostępność konkretnego kalendarza wynika z wykrycia kolekcji i praw przyznanych temu kontu. Kalendarz istniejący wyłącznie „Na moim iPhonie”, nieprzyjęte udostępnienie lub kolekcja bez zapisu nie mogą być synchronizowane przez serwer Hestii. Nie jest wymagane stałe działanie aplikacji na telefonie.

CalDAV odbiera pełny, nieograniczony datami `calendar-query REPORT` wraz z ETagami i treścią iCalendar. Nie wymaga opcjonalnego rozszerzenia `sync-collection`. Sprawdza kolekcję i uprawnienia przed odczytem i po nim; gdy dostępny `sync-token` zmieni się w trakcie, powtarza odczyt. Błędy częściowe w `207 Multi-Status`, limity wyników, błędy parsowania i braki treści nie są kompletnym inwentarzem i nie oznaczają usunięć. Utworzenie używa `If-None-Match: *`, zmiana i usunięcie `If-Match`. [CalDAV, RFC 4791](https://www.rfc-editor.org/rfc/rfc4791.html), [opcjonalna synchronizacja kolekcji, RFC 6578](https://www.rfc-editor.org/rfc/rfc6578.html).

Publiczne adresy `webcal://` i subskrypcje ICS dają odczyt, nie dwukierunkowy zapis; nie stanowią obsługiwanej konfiguracji tego modułu.

## Zakres kopiowanej treści

Kopiowane są tytuł, opis, miejsce, początek, wyłączny koniec (także zerowy czas trwania), daty całodniowe, strefy IANA, reguły cyklu, przeniesione/anulowane wystąpienia, status oraz widoczność i zajętość. Identyfikatory, wersje i czasy modyfikacji należą do mapowania dostawcy, nie do treści wydarzenia.

Wydarzenie bez podanej godziny końca ma jawną flagę `endTimeUnspecified`; pomocniczy koniec w modelu jest równy początkowi dla wydarzenia z godziną. Dla kopii Google flaga jest zachowywana w prywatnej właściwości Hestii, ponieważ natywne pole Google służy do odczytu. W CalDAV reprezentuje ją brak DTEND i własna właściwość iCalendar. Dodanie jawnego końca w zewnętrznym kliencie usuwa to znaczenie.

Zaproszenia, odpowiedzi uczestników, alarmy, konferencje i kolory nie są rozsyłane między kontami. Przy aktualizacji istniejącej kopii adapter zachowuje te dodatkowe właściwości danego dostawcy. W Google używa PATCH tylko pól synchronizowanych, w CalDAV zachowuje dodatkowe właściwości VEVENT i alarmy. Wysyłka powiadomień Google jest wyłączona (`sendUpdates=none`).

Nieobsługiwana semantyka, m.in. niestandardowa strefa czasowa, niejednoznaczny czas lokalny bez offsetu, EXRULE, okresy RDATE i RECURRENCE-ID z RANGE, zatrzymuje odczyt zamiast obcinać dane. Zdarzenia Google typu focus time, out of office i working location wymagają odrębnego modelu i zatrzymują odczyt, zamiast tracić swoje dodatkowe znaczenie. Zapis usunięcia całej serii różni się od anulowania pojedynczego wystąpienia.

Transport ma weryfikację TLS, limit czasu 30 sekund i limit odpowiedzi 32 MiB. Diagnostyka zawiera bezpieczny typ błędu/status HTTP; nie zawiera nagłówków autoryzacji, haseł, tokenów, adresów URL żądań ani treści odpowiedzi. Testy adapterów używają kontrolowanych atrap transportu; weryfikacja dostępu do rzeczywistego konta jest odrębnym etapem uruchomienia.
