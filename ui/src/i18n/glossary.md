# Budget translation glossary

This glossary defines the concepts used by `budget.*`. Locale tables record the chosen UI wording, including plural navigation labels; inflect nouns and adjectives for their sentence and CLDR category. The instalment and receipt columns give standalone terms. Receipt terminology is reserved for future UI, not a claim that receipt keys exist today.

## Meanings

| Canonical English term | Meaning | Reference key |
| --- | --- | --- |
| Budget | Private household financial plan and totals; not a bank account balance. | `budget.main.title` |
| Category | Group used to classify income or spending. | `budget.transactions.category` |
| Rule | Automatic classification condition; a manual choice takes precedence. | `budget.rules.rules` |
| One-off label | Label for one transaction in charts; not a recurring category or a one-time consent. | `budget.transactions.oneOffLabel` |
| Counterparty | Person or organisation on the other side of a transaction. | `budget.export.colCounterparty` |
| Own transfer | Movement between the owner’s accounts, including paired card repayments; not income or spending. | `budget.transactions.kinds.transfer` |
| Forecast | Estimate of future commitments; not a guaranteed future balance. | `budget.forecast.title` |
| Mortgage | Loan secured on property, represented here by private reminder schedules. | `budget.main.views.mortgages` |
| Instalment | One scheduled loan repayment; not a deposit, membership contribution or the whole loan. | Standalone term |
| Spending limit | Organisational monthly category threshold per currency; does not block bank payments. | `budget.limits.title` |
| Recurring payment | Monthly expense pattern observed at least three times; does not itself authorise payment. | `budget.recurring.title` |
| Quarantine | Bank entries excluded from totals because their status or data cannot be safely resolved. | `budget.quarantine.title` |
| Inbox | Owner’s private list of active alerts, including device and payment reminders. | `budget.main.views.inbox` |
| Consent | Permission for access to bank data, with a validity period; distinct from a login session. | `budget.inbox.kinds.consent_renewal` |
| Device | Client device linked to the owner’s private budget. | `budget.inbox.kinds.device_added` |
| Pairing code | Temporary code used to link a device; not a bank account identifier. | `budget.devices.code` |
| Receipt | Proof of purchase or payment; not income received. Reserved terminology: no receipt label exists in this budget catalog yet. | Standalone term |
| Balance | Amount held or owed on an account at a stated time; distinct from remaining budget. | `budget.main.balances` |
| Due soon | Unpaid instalment inside the configured reminder window before its due date. | `budget.mortgages.statuses.due_soon` |
| Upcoming | Future instalment outside the due-soon window; must remain distinct from due soon. | `budget.mortgages.statuses.upcoming` |
| Overdue | Unpaid instalment whose due date has passed. | `budget.mortgages.statuses.overdue` |

## Classification

| Locale | Budget | Category | Rule | One-off label | Counterparty | Own transfer |
| --- | --- | --- | --- | --- | --- | --- |
| ar | الميزانية | الفئة | القواعد | تسمية لمرة واحدة | الطرف المقابل | تحويل بين حساباتك |
| bn | বাজেট | বিভাগ | নিয়ম | এককালীন লেবেল | অপর পক্ষ | নিজের অ্যাকাউন্টগুলোর মধ্যে স্থানান্তর |
| bs | Budžet | Kategorija | Pravila | Jednokratna oznaka | Druga strana | Prijenos između vlastitih računa |
| cs | Rozpočet | Kategorie | Pravidla | Jednorázový popisek | Protistrana | Vlastní převod |
| da | Budget | Kategori | Regler | Engangsmærkat | Modpart | Egen overførsel |
| de | Budget | Kategorie | Regeln | Einmalige Bezeichnung | Gegenpartei | Eigene Umbuchung |
| el | Προϋπολογισμός | Κατηγορία | Κανόνες | Εφάπαξ ετικέτα | Αντισυμβαλλόμενος | Μεταφορά μεταξύ ιδίων λογαριασμών |
| en | Budget | Category | Rules | One-off label | Counterparty | Own transfer |
| es | Presupuesto | Categoría | Reglas | Etiqueta puntual | Contraparte | Traspaso entre cuentas propias |
| fa | بودجه | دسته | قواعد | برچسب یک‌باره | طرف مقابل | انتقال بین حساب‌های خود |
| fi | Budjetti | Luokka | Säännöt | Kertaluonteinen tunniste | Vastapuoli | Oma tilisiirto |
| fil | Badyet | Kategorya | Mga panuntunan | Minsanang label | Kabilang panig | Paglilipat sa sariling account |
| fr | Budget | Catégorie | Règles | Libellé ponctuel | Contrepartie | Virement interne |
| ga | Buiséad | Catagóir | Rialacha | Lipéad aonuaire | Contrapháirtí | Aistriú idir do chuntais |
| he | תקציב | קטגוריה | כללים | תווית חד־פעמית | צד נגדי | העברה בין חשבונות עצמיים |
| hi | बजट | श्रेणी | नियम | एकबारगी लेबल | प्रतिपक्ष | अपने खातों के बीच अंतरण |
| hr | Proračun | Kategorija | Pravila | Jednokratna oznaka | Druga strana | Prijenos između vlastitih računa |
| hu | Költségvetés | Kategória | Szabályok | Egyszeri címke | Partner | Saját átvezetés |
| hy | Բյուջե | Կատեգորիա | Կանոններ | Մեկանգամյա պիտակ | Գործարքի մյուս կողմ | Փոխանցում սեփական հաշիվների միջև |
| id | Anggaran | Kategori | Aturan | Label sekali pakai | Lawan transaksi | Transfer antar rekening sendiri |
| is | Fjárhagsáætlun | Flokkur | Reglur | Einskiptismerking | Mótaðili | Millifærsla milli eigin reikninga |
| it | Budget | Categoria | Regole | Etichetta una tantum | Controparte | Giroconto |
| ja | 予算 | カテゴリ | ルール | 単発ラベル | 取引先 | 自分の口座間の振替 |
| ko | 예산 | 분류 | 규칙 | 일회성 라벨 | 거래 상대방 | 본인 계좌 간 이체 |
| lt | Biudžetas | Kategorija | Taisyklės | Vienkartinė žyma | Sandorio šalis | Pervedimas tarp savo sąskaitų |
| lv | Budžets | Kategorija | Noteikumi | Vienreizēja atzīme | Darījuma partneris | Pārskaitījums starp saviem kontiem |
| ms | Belanjawan | Kategori | Peraturan | Label sekali sahaja | Pihak satu lagi | Pindahan antara akaun sendiri |
| my-MM | ဘတ်ဂျက် | အမျိုးအစား | စည်းမျဉ်းများ | တစ်ကြိမ်သုံး အညွှန်း | ငွေလွှဲဖက် | မိမိအကောင့်အချင်းချင်း လွှဲငွေ |
| nl | Budget | Categorie | Regels | Eenmalig label | Tegenpartij | Eigen overboeking |
| no | Budsjett | Kategori | Regler | Engangsetikett | Motpart | Egen overføring |
| pl | Budżet | Kategoria | Reguły | Etykieta jednorazowa | Kontrahent | Przelew własny |
| pt | Orçamento | Categoria | Regras | Rótulo avulso | Contraparte | Transferência própria |
| ro | Buget | Categorie | Reguli | Etichetă punctuală | Contraparte | Transfer propriu |
| ru | Бюджет | Категория | Правила | Разовая метка | Контрагент | Собственный перевод |
| sk | Rozpočet | Kategória | Pravidlá | Jednorazový popisok | Protistrana | Vlastný prevod |
| sq | Buxheti | Kategoria | Rregullat | Etiketë njëherëshe | Kundërpala | Transfertë mes llogarive tuaja |
| sr | Буџет | Категорија | Правила | Једнократна ознака | Друга страна | Пренос између сопствених рачуна |
| sv | Budget | Kategori | Regler | Engångsetikett | Motpart | Egen överföring |
| sw | Bajeti | Kategoria | Kanuni | Lebo ya mara moja | Mhusika mwingine | Uhamisho kati ya akaunti zako |
| th | งบประมาณ | หมวดหมู่ | กฎ | ป้ายกำกับครั้งเดียว | คู่ธุรกรรม | โอนระหว่างบัญชีตนเอง |
| tr | Bütçe | Kategori | Kurallar | Tek seferlik etiket | Karşı taraf | Kendi hesaplarınız arasındaki aktarım |
| uk | Бюджет | Категорія | Правила | Разова позначка | Контрагент | Власний переказ |
| vi | Ngân sách | Danh mục | Quy tắc | Nhãn dùng một lần | Đối tác giao dịch | Chuyển giữa tài khoản của mình |
| zh-Hant | 預算 | 類別 | 規則 | 單次註記 | 交易對象 | 本人帳戶間轉帳 |
| zh | 预算 | 类别 | 规则 | 单次标签 | 交易对方 | 本人账户间转账 |

## Planning and payments

| Locale | Forecast | Mortgage | Instalment | Spending limit | Recurring payment | Quarantine |
| --- | --- | --- | --- | --- | --- | --- |
| ar | التوقعات | القروض العقارية | قسط | حدود الإنفاق | دفعات متكررة | معاملات قيد المراجعة |
| bn | পূর্বাভাস | গৃহঋণ | কিস্তি | খরচের সীমা | নিয়মিত পেমেন্ট | পর্যালোচনার অপেক্ষায় |
| bs | Prognoza | Hipotekarni krediti | rata | Limiti potrošnje | Ponavljajuća plaćanja | Karantena |
| cs | Prognóza | Hypotéky | splátka | Výdajové limity | Pravidelné platby | Karanténa |
| da | Prognose | Boliglån | ydelse | Forbrugsgrænser | Faste betalinger | Karantæne |
| de | Prognose | Hypotheken | Rate | Ausgabenlimits | Wiederkehrende Zahlungen | Quarantäne |
| el | Πρόβλεψη | Στεγαστικά δάνεια | δόση | Όρια δαπανών | Επαναλαμβανόμενες χρεώσεις | Προς έλεγχο |
| en | Forecast | Mortgages | instalment | Spending limits | Recurring charges | Quarantine |
| es | Previsión | Hipotecas | cuota | Límites de gasto | Pagos recurrentes | Cuarentena |
| fa | پیش‌بینی | وام‌های مسکن | قسط | سقف‌های هزینه | پرداخت‌های تکرارشونده | تراکنش‌های در انتظار بررسی |
| fi | Ennuste | Asuntolainat | maksuerä | Kulutusrajat | Toistuvat maksut | Karanteeni |
| fil | Pagtataya | Mga pautang sa bahay | hulog | Mga limitasyon sa paggastos | Paulit-ulit na bayarin | Kuwarentenas |
| fr | Prévisions | Prêts immobiliers | mensualité | Plafonds de dépenses | Paiements récurrents | Quarantaine |
| ga | Réamhaisnéis | Morgáistí | tráthchuid | Teorainneacha caiteachais | Íocaíochtaí rialta | Coraintín |
| he | תחזית | משכנתאות | תשלום | תקרות הוצאה | חיובים חוזרים | עסקאות לבדיקה |
| hi | पूर्वानुमान | गृह ऋण | किस्त | खर्च की सीमाएँ | नियमित भुगतान | समीक्षा हेतु अलग |
| hr | Prognoza | Hipotekarni krediti | rata | Limiti potrošnje | Ponavljajuća plaćanja | Karantena |
| hu | Előrejelzés | Jelzáloghitelek | törlesztőrészlet | Kiadási keretek | Rendszeres fizetések | Karantén |
| hy | Կանխատեսում | Հիպոթեքային վարկեր | վճարամաս | Ծախսերի սահմանաչափեր | Պարբերական գանձումներ | Ստուգման սպասող |
| id | Proyeksi | Kredit rumah | angsuran | Batas pengeluaran | Pembayaran rutin | Karantina |
| is | Spá | Íbúðalán | afborgun | Útgjaldamörk | Reglulegar greiðslur | Í bið eftir yfirferð |
| it | Previsione | Mutui | rata | Limiti di spesa | Addebiti ricorrenti | Quarantena |
| ja | 予測 | 住宅ローン | 返済 | 支出上限 | 定期支払い | 確認待ち取引 |
| ko | 예측 | 주택담보대출 | 회차별 납입금 | 지출 한도 | 정기 결제 | 검토 대기 거래 |
| lt | Prognozė | Būsto paskolos | įmoka | Išlaidų limitai | Periodiniai mokėjimai | Karantinas |
| lv | Prognoze | Hipotekārie kredīti | maksājums | Tēriņu limiti | Regulārie maksājumi | Karantīna |
| ms | Unjuran | Pinjaman perumahan | ansuran | Had perbelanjaan | Bayaran berulang | Kuarantin |
| my-MM | ခန့်မှန်းချက် | အိမ်ရာချေးငွေများ | အရစ်ကျငွေ | အသုံးစရိတ် ကန့်သတ်ချက်များ | ပုံမှန်ပေးချေမှုများ | စိစစ်ရန်ဆိုင်းငံ့ |
| nl | Prognose | Hypotheken | termijn | Uitgavenlimieten | Terugkerende afschrijvingen | Quarantaine |
| no | Prognose | Boliglån | termin | Forbruksgrenser | Faste betalinger | Karantene |
| pl | Prognoza | Hipoteka | rata | Limity wydatków | Powtarzalne wydatki | Kwarantanna |
| pt | Previsão | Financiamentos imobiliários | parcela | Limites de gastos | Cobranças recorrentes | Quarentena |
| ro | Prognoză | Credite ipotecare | rată | Limite de cheltuieli | Plăți recurente | Carantină |
| ru | Прогноз | Ипотеки | платёж | Лимиты расходов | Регулярные платежи | Карантин |
| sk | Prognóza | Hypotéky | splátka | Výdavkové limity | Pravidelné platby | Karanténa |
| sq | Parashikimi | Kreditë hipotekare | këst | Kufijtë e shpenzimeve | Pagesa periodike | Në pritje të shqyrtimit |
| sr | Прогноза | Хипотекарни кредити | рата | Лимити потрошње | Понављајућа плаћања | Карантин |
| sv | Prognos | Bolån | delbetalning | Utgiftsgränser | Återkommande debiteringar | Karantän |
| sw | Makadirio | Mikopo ya nyumba | awamu | Vikomo vya matumizi | Malipo yanayojirudia | Yanayosubiri ukaguzi |
| th | การคาดการณ์ | สินเชื่อบ้าน | ค่างวด | วงเงินใช้จ่าย | รายจ่ายประจำ | รอตรวจสอบ |
| tr | Öngörü | Konut kredileri | taksit | Harcama limitleri | Düzenli ödemeler | İnceleme bekleyenler |
| uk | Прогноз | Іпотеки | платіж | Ліміти витрат | Регулярні платежі | Карантин |
| vi | Dự báo | Khoản vay mua nhà | khoản trả góp | Hạn mức chi tiêu | Khoản chi định kỳ | Chờ kiểm tra |
| zh-Hant | 預測 | 房貸 | 期款 | 支出上限 | 定期扣款 | 待審核交易 |
| zh | 预测 | 房贷 | 分期还款 | 支出限额 | 定期付款 | 待审核交易 |

## Access and records

| Locale | Inbox | Consent | Device | Pairing code | Receipt | Balance |
| --- | --- | --- | --- | --- | --- | --- |
| ar | صندوق الوارد | موافقة مصرفية | جهاز | رمز الاقتران | إيصال | الأرصدة |
| bn | বিজ্ঞপ্তির বাক্স | ব্যাংকের সম্মতি | ডিভাইস | পেয়ারিং কোড | রসিদ | স্থিতি |
| bs | Obavijesti | Saglasnost banci | Uređaj | Kod za uparivanje | fiskalni račun | Stanja |
| cs | Oznámení | Bankovní souhlas | Zařízení | Párovací kód | účtenka | Zůstatky |
| da | Indbakke | Banksamtykke | Enhed | Parringskode | kvittering | Saldi |
| de | Posteingang | Bankeinwilligung | Gerät | Kopplungscode | Beleg | Salden |
| el | Εισερχόμενα | Τραπεζική συγκατάθεση | Συσκευή | Κωδικός σύζευξης | απόδειξη | Υπόλοιπα |
| en | Inbox | Bank consent | Device | Pairing code | receipt | Balances |
| es | Bandeja de entrada | Consentimiento bancario | Dispositivo | Código de vinculación | recibo | Saldos |
| fa | صندوق ورودی | رضایت‌نامهٔ بانکی | دستگاه | کد جفت‌سازی | رسید | مانده‌ها |
| fi | Saapuneet | Pankkisuostumus | Laite | Parituskoodi | kuitti | Saldot |
| fil | Mga abiso | Pahintulot sa bangko | Aparato | Code sa pagpapares | resibo | Mga balanse |
| fr | Boîte de réception | Autorisation bancaire | Appareil | Code d'association | reçu | Soldes |
| ga | Bosca isteach | Toiliú bainc | Gléas | Cód péireála | admháil | Iarmhéideanna |
| he | תיבת דואר נכנס | הסכמה לבנק | מכשיר | קוד צימוד | קבלה | יתרות |
| hi | सूचना पेटी | बैंक की सहमति | डिवाइस | पेयरिंग कोड | रसीद | शेष राशियाँ |
| hr | Obavijesti | Privola banci | Uređaj | Kod za uparivanje | fiskalni račun | Stanja |
| hu | Értesítések | Banki hozzájárulás | Eszköz | Párosítási kód | nyugta | Egyenlegek |
| hy | Մուտքային ծանուցումներ | Բանկային համաձայնություն | Սարք | Զուգակցման կոդ | անդորրագիր | Մնացորդներ |
| id | Kotak masuk | Persetujuan bank | Perangkat | Kode pemasangan | struk | Saldo |
| is | Innhólf | Bankasamþykki | Tæki | Pörunarkóði | kvittun | Reikningsstöður |
| it | Notifiche | Consenso bancario | Dispositivo | Codice di abbinamento | ricevuta | Saldi |
| ja | 通知一覧 | 銀行への同意 | デバイス | ペアリングコード | レシート | 残高 |
| ko | 알림함 | 은행 동의 | 기기 | 페어링 코드 | 영수증 | 잔액 |
| lt | Pranešimai | Banko sutikimas | Įrenginys | Susiejimo kodas | kvitas | Likučiai |
| lv | Iesūtne | Bankas piekrišana | Ierīce | Pārošanas kods | čeks | Atlikumi |
| ms | Peti masuk | Persetujuan bank | Peranti | Kod berpasangan | resit | Baki |
| my-MM | အသိပေးစာဝင်ပုံး | ဘဏ်သဘောတူခွင့်ပြုချက် | စက် | တွဲချိတ်ကုဒ် | ပြေစာ | လက်ကျန်များ |
| nl | Meldingen | Banktoestemming | Apparaat | Koppelcode | bon | Saldi |
| no | Innboks | Banksamtykke | Enhet | Paringskode | kvittering | Saldoer |
| pl | Skrzynka | Zgoda bankowa | Urządzenie | Kod parowania | paragon | Salda |
| pt | Notificações | Consentimento bancário | Dispositivo | Código de pareamento | recibo | Saldos |
| ro | Notificări | Consimțământ bancar | Dispozitiv | Cod de asociere | bon fiscal | Solduri |
| ru | Входящие | Банковское согласие | Устройство | Код сопряжения | чек | Остатки |
| sk | Oznámenia | Bankový súhlas | Zariadenie | Párovací kód | pokladničný doklad | Zostatky |
| sq | Kutia hyrëse | Pëlqim bankar | Pajisje | Kodi i çiftimit | dëftesë pagese | Gjendjet |
| sr | Обавештења | Сагласност банци | Уређај | Код за упаривање | фискални рачун | Стања |
| sv | Inkorg | Banksamtycke | Enhet | Parkopplingskod | kvitto | Saldon |
| sw | Kikasha | Idhini ya benki | Kifaa | Msimbo wa kuoanisha | risiti | Salio |
| th | กล่องแจ้งเตือน | ความยินยอมของธนาคาร | อุปกรณ์ | รหัสจับคู่ | ใบเสร็จ | ยอดคงเหลือ |
| tr | Bildirim kutusu | Banka izni | Cihaz | Eşleştirme kodu | makbuz | Bakiyeler |
| uk | Вхідні | Банківська згода | Пристрій | Код сполучення | чек | Залишки |
| vi | Hộp thông báo | Chấp thuận ngân hàng | Thiết bị | Mã ghép nối | biên lai | Số dư |
| zh-Hant | 通知匣 | 銀行授權 | 裝置 | 配對碼 | 收據 | 餘額 |
| zh | 通知收件箱 | 银行授权 | 设备 | 配对码 | 收据 | 余额 |

## Payment status

| Locale | Due soon | Upcoming | Overdue |
| --- | --- | --- | --- |
| ar | مستحق قريبًا | قادم | متأخر |
| bn | শীঘ্রই প্রদেয় | আসন্ন | সময় পেরিয়ে গেছে |
| bs | Uskoro dospijeva | Nadolazeće | Dospjelo |
| cs | Brzy splatné | Nadcházející | Po splatnosti |
| da | Forfalder snart | Kommende | Forfalden |
| de | Bald fällig | Anstehend | Überfällig |
| el | Λήγει σύντομα | Επερχόμενη | Εκπρόθεσμη |
| en | Due soon | Upcoming | Overdue |
| es | Vence pronto | Próximo | Vencido |
| fa | سررسید نزدیک | پیش رو | سررسید گذشته |
| fi | Erääntyy pian | Tuleva | Erääntynyt |
| fil | Malapit nang takda | Paparating | Lampas na sa takdang petsa |
| fr | Échéance proche | À venir | En retard |
| ga | Dlite go luath | Le teacht | Thar téarma |
| he | לתשלום בקרוב | עתידי | באיחור |
| hi | जल्द देय | आगामी | समय-सीमा बीती |
| hr | Uskoro dospijeva | Nadolazeće | Dospjelo |
| hu | Hamarosan esedékes | Közelgő | Késedelmes |
| hy | Ժամկետը մոտենում է | Առաջիկա | Ուշացած |
| id | Segera jatuh tempo | Mendatang | Lewat jatuh tempo |
| is | Brátt á gjalddaga | Væntanlegt | Í vanskilum |
| it | In scadenza | In arrivo | In ritardo |
| ja | まもなく期限 | 予定あり | 支払期限超過 |
| ko | 납부기한 임박 | 예정 | 기한 지남 |
| lt | Terminas netrukus | Artėja | Pradelsta |
| lv | Termiņš drīzumā | Gaidāms | Nokavēts |
| ms | Perlu dibayar tidak lama lagi | Akan datang | Tertunggak |
| my-MM | မကြာမီ ပေးချေရမည် | လာမည် | ရက်လွန်နေသည် |
| nl | Binnenkort verschuldigd | Komend | Achterstallig |
| no | Forfaller snart | Kommende | Forfalt |
| pl | Wkrótce termin | Nadchodząca | Po terminie |
| pt | Vence em breve | Próximo | Em atraso |
| ro | Scadent în curând | Viitoare | Restant |
| ru | Скоро к оплате | Предстоящие | Просрочено |
| sk | Čoskoro splatné | Nadchádzajúce | Po splatnosti |
| sq | Afati së shpejti | Të ardhshme | Me vonesë |
| sr | Ускоро доспева | Предстојеће | Доспело |
| sv | Förfaller snart | Kommande | Förfallen |
| sw | Inatakiwa karibuni | Inayokuja | Imechelewa |
| th | ใกล้ครบกำหนด | ยังไม่ถึงกำหนด | เกินกำหนด |
| tr | Son ödeme yaklaşıyor | Yaklaşan | Gecikmiş |
| uk | Скоро до сплати | Майбутні | Прострочено |
| vi | Sắp đến hạn | Sắp tới | Quá hạn |
| zh-Hant | 即將到期 | 待到期 | 已逾期 |
| zh | 即将到期 | 待到期 | 已逾期 |

## Punctuation and quotation marks

- Preserve interpolation tokens such as `{value}` and machine-readable formats such as `YYYY-MM`, ISO currency codes, IBAN, UTC, API, QR, MCC, PoC, CSV and XLSX. Hestia and Vesta are brand names. Established native loanwords and cognates are not untranslated fallback strings.
- Arabic and Persian: use `،` for commas, `؛` for semicolons and `؟` for questions. Keep colons and parentheses in their usual forms. Use `«…»` for quoted text; do not replace punctuation inside technical identifiers.
- Hebrew: normal Hebrew prose uses commas, colons, periods and question marks in their usual forms. This catalog uses typographic `“…”` for quotations; geresh and gershayim serve Hebrew abbreviations and are not an automatic replacement for quote delimiters.
- Japanese: use `、。`, full-width `：` and `（）`, and primary `「…」` quotation marks. Keep ASCII placeholders, currency codes and numeric formats intact.
- Simplified Chinese: use `，。；：？！` and `（）`, with `“…”` quotations. Traditional Chinese (Taiwan): the same full-width punctuation, with primary `「…」` quotations. Do not insert spaces immediately after full-width punctuation.
- Korean: use ordinary commas, periods and parentheses, and `“…”` quotations; do not apply Chinese or Japanese full-width punctuation automatically.
- French: use U+00A0 NO-BREAK SPACE before `: ; ? !`, and inside `« … »`; use non-breaking separation before `%` in prose. Preserve syntax and placeholders.
- Armenian: use `։` for sentence endings, `․` for clause separation and `՝` for label/explanatory separation, with `«…»` quotations. Thai prose normally separates sentences with spaces instead of ASCII periods. Burmese uses `၊` and `။`.
- Bengali and Hindi: sentence-final danda (`।`) is appropriate for prose; commas and parentheses remain ordinary. Other South and Southeast Asian scripts follow their own sentence punctuation; do not impose a single CJK or Arabic rule on them.
- Use non-breaking separation before percentages where required by the locale, including Czech, German, French, Spanish and the reviewed Nordic catalogs.
- For other Latin, Greek, Armenian and Cyrillic locales, use normal locale punctuation and the quote pairs below. Locale register follows the surrounding catalog; Spanish uses tú, Portuguese follows the existing Brazilian você register.
- A count category is grammatical, not a numeric range shared by all languages. Review every category used by `Intl.PluralRules(locale)`; repeated forms are correct for languages without that distinction. Keep unused categories valid fallback text. Where a nonplural key contains a variable count, prefer a neutral label such as “Entries: {count}” if the noun cannot agree for every number.

| Locale | Primary quote example |
| --- | --- |
| ar | «…» |
| bn | “…” |
| bs | „…“ |
| cs | „…“ |
| da | »…« |
| de | „…“ |
| el | «…» |
| en | “…” |
| es | «…» |
| fa | «…» |
| fi | ”…” |
| fil | “…” |
| fr | « … » |
| ga | “…” |
| he | “…” |
| hi | “…” |
| hr | „…“ |
| hu | „…” |
| hy | «…» |
| id | “…” |
| is | „…“ |
| it | «…» |
| ja | 「…」 |
| ko | “…” |
| lt | „…“ |
| lv | “…” |
| ms | “…” |
| my-MM | “…” |
| nl | “…” |
| no | «…» |
| pl | „…” |
| pt | “…” |
| ro | „…” |
| ru | «…» |
| sk | „…“ |
| sq | “…” |
| sr | „…“ |
| sv | ”…” |
| sw | “…” |
| th | “…” |
| tr | “…” |
| uk | «…» |
| vi | “…” |
| zh-Hant | 「…」 |
| zh | “…” |

## Consistency checks

- Keep upcoming, due soon, overdue and completed/paid distinct. “Paid” markers represent the owner’s recorded status, not a bank payment initiated by this screen.
- Keep pending, booked/confirmed, on hold, rejected and cancelled distinct. Transfer rejection means “not an own transfer”, not necessarily a rejected bank payment.
- Daily rate in the spending-pace panel means average daily spending, never an interest rate, fee or daily wage.
- Refund means returned money, not a new expense. Own transfers are excluded from income and spending when confirmed.
- The card note must refer to the same unpaid card spending as its title. Do not replace it with an unexplained risk/exposure term.
- Limits, forecasts and mortgage reminders are informational here. Translations must not imply that they block payments, initiate payments or send notifications.
