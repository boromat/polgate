# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-13

A leak evaluation now measures what reaches the provider, and the polanon it requires, 0.3.3,
fixes every leak the evaluation found. The hub also runs on Windows with a double click. A review
of the whole project before the release fixed seven faults, among them a text file saved as UTF-16
reaching the provider unmasked.

### Added

- **`start.bat` for Windows**: double-click it to run the hub. The first run creates `.venv`,
  installs the hub and the spaCy model and writes `.env` with generated keys; every run checks the
  database schema, starts the hub and opens it in the browser. It takes the same arguments as
  `run.sh`, plus `--no-browser`.
- **A leak evaluation** in `evals/`: 27 made-up conversations, with attachments, surnames in
  another case and a mistyped PESEL, played through the running hub against a local provider that
  records every request. It counts the listed values that reach the provider, ordinary phrases
  masked when they should not be, refused turns and answers not restored exactly. Its first run,
  on polanon 0.2.2, found 4 of 125 values leaking and 2 phrases masked by mistake, all detection
  gaps that polanon 0.3.3 fixed; with it, nothing leaks. CI runs it with the mock judge and fails
  on any finding `evals/known_gaps.json` does not list.

### Changed

- **polanon 0.3.3 or later is required.** It fixes the four leaks and two words masked by mistake
  that the leak evaluation found: a password after "Hasło administratora:", a PESEL with a typo
  after its label, a feminine surname in another case after the full name ("Bielskiej"), a street
  with neither "ul." nor a postcode, and "ZUS" and "Płatnik" taken for company names (0.3.3). It
  also keeps a known value whole where a new detection only partly overlaps it (0.3.1), and its
  `NerModelUnavailableError` is still a `ModuleNotFoundError`, so the hub handles a missing spaCy
  model as before (0.3.0). From the earlier releases: its output sweep no longer reports a restored name the
  model inflected, such as "Jan Kowalskia", as new personal data, and it masks phone numbers
  written with a bare 48 prefix and e-mail addresses with Polish letters (0.2.1). It also masks
  links to a person's profile as a new type, `LINK`, which the admin panel lists and an existing
  policy switches on; a surname or a profile handle on its own once the full name or link was
  masked; birth dates written in English; e-mail addresses with "[at]"; and a street without
  "ul." before a postcode (0.2.2). See
  [its changelog](https://github.com/boromat/polanon/blob/main/CHANGELOG.md).

### Fixed

- **A text file saved as UTF-16 is masked.** PowerShell 5.1, Notepad's "Unicode" and Excel's
  "Unicode Text" write UTF-16. The hub read such a `.txt`, `.csv` or `.json` as cp1250, with a NUL
  between every letter, and the guards then missed the PESEL and the e-mail address in it, while
  the provider, which received that text, read it without trouble. The byte-order mark now decides
  the encoding, UTF-16 without one is recognised by its NUL bytes, and text that still holds a NUL
  after decoding is refused as unreadable.
- **A small XLSX can no longer keep a worker busy for minutes.** A 5 KB workbook with cells in A1
  and XFD1048576 made the reader pad 17 billion empty cells, about 14 minutes of CPU on a thread
  every other upload and turn shares. The declared dimension is now ignored, trailing empty cells
  are dropped, and a workbook is read for at most two million cells, with the usual truncation
  marker after them.
- **Retention does what the admin panel says.** A typo, a zero or a negative number in a retention
  field meant "keep forever", so a slip of the keyboard switched retention off; it now keeps the
  value that was set, and only an empty field means forever. A number such as 999999 made every
  retention pass fail on a date out of range; periods are now capped at a hundred years. And a
  shorter mapping retention applied only to mappings stored afterwards, so switching "forever" to
  30 days kept every existing mapping forever; changing it now sets the expiry of every stored
  mapping from the day it was stored.
- **A provider error always reaches the page.** The audit row of a failed provider call held the
  first 200 characters of the provider's answer, and the audit refuses any row that contains a
  masked value. An answer such as "Limit 30000" holds the PIN 3000, so the row was refused inside
  the error handler, and the stream ended without telling the employee anything. The row now holds
  the status code ("status=429"), and a refused payload's row the token it held.
- **The review step says when the judge is down.** With approval before sending switched on, a
  stage-2 judge that failed while the policy said to block ended the preview in a server error, with
  nothing in the audit trail. It now gives the same message as the answer stream, "nothing was
  sent", and records `turn_blocked_judge`.
- **The retention pass no longer fails on a conversation somebody is writing in.** An old
  conversation whose messages had all expired counted as empty even with a new message waiting to
  stream, and its delete could reach the database before the rows that refer to it, which the
  foreign keys refused. The whole pass rolled back, after the bytes of expired uploads had already
  been removed. A waiting message now keeps its conversation, and a conversation's vault rows are
  deleted before it.
- **An empty key file is never used as a key.** A generated secret's file exists a moment before
  its contents, so a second request on a fresh hub could read it empty, and a process killed in
  that moment left it empty for good. An empty session key signs a cookie anybody can make. A
  reader now waits briefly for the contents, and a file that stays empty stops the hub with a
  message saying to delete it; nothing was ever signed or encrypted with it.
- **An existing installation gets the well-known names a polanon release adds.** A policy copies
  polanon's `WELL_KNOWN_NAMES` into its list of names never to mask when it is created, so "ZUS",
  "Płatnik" and the other names polanon 0.3.3 added would have reached new installations only and
  stayed masked as company names everywhere else. The policy now records which names it has been
  offered, the way it records entity types, and a name added since joins the list on the next
  start; one the administrator deleted stays deleted, and the audit trail records how many were
  added. The new column is added at startup like any other.
- **A short surname masked on its own no longer refuses an unrelated message.** polanon 0.2.2
  masks a surname such as "Lis" wherever it stands alone, and the check before every provider call
  then found it inside "Lista" or "Kotłownia" and refused to send. A one-word value shorter than
  four letters now counts as a word of its own or with a Polish case ending ("Lis", "Lisa"), as
  polanon matches it, and not inside another word.

## [0.1.0] - 2026-11-03

The first release: the hub, as source and as a Docker image on GitHub Container Registry. The
masking engine is [polanon](https://github.com/boromat/polanon), installed from
[PyPI](https://pypi.org/project/polanon/) as `polanon[ner,judge]>=0.2.0` rather than kept as a copy
inside this repository; its tests, benchmark, examples and packaging live in its repository, and its
own changes are recorded in [its changelog](https://github.com/boromat/polanon/blob/main/CHANGELOG.md).
Everything below is the work that led to this release, including the detection work done here
before the engine moved to polanon.

### Added

- **Birth dates.** A date after "ur.", "urodzona", "data urodzenia", "date of birth", "DOB" or
  "geb." is its own type, `BIRTH_DATE` (`<DATA_URODZENIA_1>`), on by default, so it stays masked
  where ordinary dates are left readable. An existing hub policy switches it on at startup.
- **Foreign invoices.** `InternationalGuard` masks what the Polish rules could not read on the
  owner's British, American, Swedish and Swiss sample invoices: EU, UK, Swiss and Norwegian VAT
  numbers, company register numbers, Bankgiro, Plusgiro, sort codes and account numbers,
  international and British phone numbers, street addresses and postcodes from Britain, the US,
  Canada and continental Europe, companies by their foreign legal form (Ltd, Inc, GmbH, AB), and
  names after a form of address, a label such as "Attn" or "Vår referens", or a closing formula.
  Almost none of these values has a checksum, so each rule fires on the label or the official shape
  that vouches for the value. Three entity types are new — `TAX_ID`, `COMPANY_ID` and
  `BANK_ACCOUNT` — on by default and switched on in existing installations. On the owner's
  twenty-four samples nothing is refused any more and only city names still reach the provider.
- **Names that are never masked.** The Polish model tags "Facebooka", "Canvie" and "Google Ads" as
  companies and places, so a marketing team's "zrób post na Facebooku" reached the provider as
  "zrób post na <FIRMA_1>". The policy page now has a "never mask" list beside the "always mask"
  one, filled with the platforms and tools an office talks about and matched in every Polish case
  form. It releases only what a model or the judge took for a name, and only when every word of the
  detection is on the list: identifiers, e-mail addresses, the denylist and values a conversation
  has already masked stay masked. An existing installation gets the default list on upgrade.
- **What a message sent, right under it.** As soon as a message is masked the page shows how many
  values became tokens, or plainly "nothing masked", with the same "what the model saw" preview a
  reloaded conversation offers: a message that went out unmasked no longer looks like any other.
  Enter sends and Shift+Enter starts a new line, as in every other chat window.
- **`PersonNameGuard`, names typed the way people type.** Polish first names in all seven cases, in
  lowercase, in capitals and without Polish letters, together with the surname that agrees with them
  ("kamilem kowalczykiem", "ani kowalskiej") or the surname after "pan", "pani", "dr". It needs no
  language model. Forms that are also ordinary words ("marka", "mają", "ani") count only next to a
  surname or a form of address.
- **Messages that say why they were stopped.** A turn refused by the final check, or held because
  the stage-2 judge is down, explains itself and says nothing was sent, instead of showing the
  generic error that invites pressing send again.
- **The hub.** Local accounts with argon2id passwords, conversations with streamed answers,
  file uploads (PDF, DOCX, XLSX, CSV, JSON, TXT, images through optional OCR), a per-message preview
  of what the provider actually received, an admin panel for policy, users, audit and statistics,
  and a PL/EN interface.
- **Two-stage masking.** Deterministic guards for Polish identifiers (PESEL, NIP, REGON, ID card,
  IBAN and payment cards by checksum; KRS by its label; passport by its format), contact details,
  addresses, device identifiers, vehicle plates and VINs, technical secrets and an admin denylist;
  then an optional LLM judge that reviews the already-masked text for what still identifies
  someone.
- **`CompanyGuard`**, which finds Polish company names by their legal form (`Sp. z o.o.`, `S.A.`,
  `sp. j.`, `s.c.`, and `Fundacja` / `Kancelaria` / `Zakłady` in front) instead of asking a model to
  recognise a name word by word.
- **Reversible masking.** A Fernet-encrypted vault of token to original, stable tokens within a
  conversation, restoration inside the SSE stream without splitting a token across chunks, and an
  output sweep for invented tokens and personal data nobody sent.
- **Retention that deletes.** Vault mappings, message history, uploaded files and their bytes, and
  queued turns whose stream never started, all removed on the schedule the admin sets.
- **Account management.** Employees change their own password and interface language; the admin can
  reset a password, and the panel warns while an account still has its first password.
- **Browser-side hardening.** Content-Security-Policy with no `unsafe-inline` and no `unsafe-eval`,
  an origin check on state-changing requests, `SameSite=Lax` cookies with a configurable `Secure`
  flag, HSTS, sign-in throttling, and `?next=` restricted to paths inside the hub.
- **Schema reconciliation at startup** (`polgate/schema.py`). `create_all` never alters an existing
  table, so a field added to a model left every earlier database one column short and the first
  query naming it failed. Missing columns are added and backfilled with the field's default; drops,
  renames and retypes are reported rather than guessed. `python -m polgate.schema` prints what the
  database and the models disagree about, and `--drop-orphan-columns` clears leftovers after saying
  what they hold.
- Threat model, GDPR notes, architecture and contribution docs; MIT license; Docker Compose;
  `run.sh`, which checks the environment before starting the hub.
- **Error pages.** An `HTTPException` reaches a browser as a translated page with a way back, an
  HTMX swap as one translated line, and a script as the JSON it always got. The developer-facing
  detail ("admin only", "nothing to stream") is no longer shown to anybody.
- **A readiness probe that reads the database.** `/health` counts the accounts and answers 503 when
  it cannot, so the container's health status reports real storage trouble, not only that the
  process is up.
- **A context budget.** `HISTORY_BUDGET_CHARS` and `ATTACHMENT_BUDGET_CHARS` cap what is replayed to
  the provider each turn.

### Changed

- **Dates are left readable by default.** Masking every deadline and invoice date left the model
  unable to answer what the text was sent for, which is why the hub already turned them off. A birth
  date is a separate type and stays masked.
- **One value, one token.** A value read as two types in two places ("WA 12345" as a postcode, then
  after "nr rej." as a registration) gets one token, of the type the guards were surest of.
- **A detector added by a later release is on for an installation that already exists.** The policy
  stores the entity types that are switched on, so a new type would have stayed off wherever a
  policy row was already saved, with nothing on screen to say so. The hub now remembers which types
  a policy has been offered and switches on the new ones a fresh policy would have, recording that
  in the audit trail. A type the administrator unticked stays off, and a policy with masking
  switched off stays off. The defaults are derived from the library's list of types, minus the
  ones deliberately off (dates): with a second hand-written list, a hub that reloaded between the
  two edits recorded the new types as offered and never switched them on.
- SQLite connections now use WAL, a five-second busy timeout and `foreign_keys=ON`. Readers no longer
  block behind a writer, an overlap waits instead of failing, and orphaned rows are refused. Turning
  the constraints on immediately caught test fixtures that were building conversations owned by an
  account that did not exist.
- **A policy that masks nothing now says so.** Clearing every entity type in the admin panel is a
  legitimate setting and the most damaging mis-click available, and the two looked identical: the
  chat page kept showing a green "masking on" badge while raw text went to the provider. The badge
  turns red, the panel and the dashboard warn, and the audit row says `masking_disabled`.
- **A hostile judge answer can no longer garble a message.** A finding is replaced at every
  occurrence, so a one-letter answer from a small model would have hit the inside of words.
  Findings shorter than three characters, and findings that are only an existing token, are dropped.
- **CI** runs read-only and cancels superseded runs. A test checks that the masking engine runs on
  the standard library alone by looking at what each of its modules imports at import time.
- **The guards no longer block the server.** They are synchronous CPU work — regex over the whole
  document, plus a language model — and they ran inline inside an async handler, so one 200 000
  character upload froze every other employee's request for over a second. They run in a thread now,
  as does the output sweep. Masking the same document also got about three times faster, because the
  masked text is rebuilt in one pass instead of once per span.
- **A blank message is refused** before it becomes a paid provider call. `required` on a textarea
  only rejects an empty string; a browser submits a line of spaces without complaint.
- **A malformed form** reaches a browser as a page rather than as FastAPI's list of field errors.
- **More detection**, found by probing with realistic Polish rather than by reading the code: IPv6
  addresses in every written form, phone numbers dialled as `0048…`, and passwords introduced by a
  word rather than by punctuation (`hasło Zima2026`) — the last only when the value looks like a
  credential, so `Hasło zostało zmienione` is left alone. Polish double surnames come back as one
  person instead of `<OSOBA_1>-<OSOBA_2>`, and a heading made only of form words is no longer a name.
- The Docker image builds in two stages, runs as a non-root user, declares a healthcheck, and ships
  with a `.dockerignore` that keeps `data/` and its vault key out of the build context.
- First run against a real provider, with the outgoing request captured at the HTTP transport. For
  a Polish document no masked value left. English, UK and Swiss invoices leaked a lot, because the
  guards did not know those formats; that run is what led to `InternationalGuard` (see *Added*).

### Removed

- Presidio. Its `SpacyRecognizer` maps the OntoNotes labels while the Polish models emit the NKJP
  set, so it silently returned nothing; the masking engine talks to spaCy directly instead.

### Fixed

- **Error messages never reached the page.** HTMX 2 leaves a 4xx or 5xx answer out of the page by
  default, so "unsupported file type", "file too large", the refusal of a message of spaces and
  every translated error line were sent and never shown: the button simply did nothing. Error
  answers are swapped in now; a form that failed keeps what was typed. A request made after the
  session ended swapped the whole login page into the chat; it now takes the browser to the login
  page, which returns to the conversation afterwards.
- **A provider that needs no key could not be reached.** With `LLM_API_KEY` empty, as it is on a
  first run and for a local Ollama endpoint, the client still sent `Authorization: Bearer` with
  nothing after it, which the HTTP library refuses to send, so every turn ended in the generic
  provider error. The header is left out when no key is set.
- **A scanned PDF failed wherever OCR was installed.** A PDF without a text layer was handed to
  Pillow, which cannot open PDFs, so the Docker image, the one place tesseract ships, refused every
  scan with a server error. The pictures a scanner stores in each page are read instead, page by
  page.
- **A large upload held every other employee's requests.** Text extraction ran on the event loop
  and read the whole document before cutting it to 200,000 characters, so a 9.9 MB spreadsheet
  export held every page and answer stream in the hub for 17 seconds. Extraction runs in a thread
  now and stops reading rows, paragraphs and pages once it has the text it keeps: the same upload
  takes 6 seconds and nobody else waits.
- **A masked value spelled differently went out in the clear.** Known values were matched letter
  for letter, while the guards read through no-break spaces, invisible characters and decomposed
  accents: "pani Ziaja" was masked and "ZIAJA", or the name with a zero-width space or a soft hyphen
  in it, was not, in the same text or in the next turn, and `mask_known_values` let them all
  through. Every occurrence of a known value is now found as written and also the way the guards
  read: in capitals, across line breaks and runs of spaces, through invisible characters, and for
  an identifier with its groups written apart or together. Longer values are placed first in every
  spelling, so a shorter known value no longer cuts a longer one in two. A capitalised one-word name
  in all lowercase is still left alone, since many Polish surnames are also ordinary words, but a
  lowercase spelling a detector found keeps a token of its own and is masked from then on;
  identifiers match in any case. Spellings of one value share its token instead of reaching the
  model as several people, while a password and a name that read alike keep two, so the name is
  still found in capitals later. The output sweep reads answers the same way, and `entity_counts`
  follow the token a value keeps.
- **A known value was left in the clear where a detector had found a piece of it.** A signature
  with "Jan" and "Kowalski" on two lines was read as "Jan" by the name rule, which sent the
  surname; a known password holding an e-mail address went out as `<EMAIL_1>#!` in a later turn;
  an IBAN broken over two lines lost to a card number read inside it. An occurrence of a known
  value now takes in what a detector found inside it and is masked whole.
- **A short secret leaked through dates.** A four-digit PIN of "2025" became a known value, so
  "15.04.2025" went out as "15.04.<SEKRET_1>", which told the model the PIN was a year, and "20250"
  was widened into a second secret. A number shorter than six digits is now left alone inside a
  longer number, a date, a time, an amount or a span of years, also where typesetting put a dash in
  it ("2024–2025"). Where it stands alone, as in "w 2025 roku", next to letters, as in "bank2025",
  or next to any other number, as in "PIN/PUK 4821/36912874", it is still masked.
- **Part of a password went out when it held an address.** `hasło: "jan.kowalski@example.com#!"` was
  sent as `"<EMAIL_1>#!"`. A secret is now masked whole with whatever was detected inside it, and
  two passwords that differ only in letter case stay two secrets.
- **A placeholder written in one turn became real data in the next.** A literal `<PESEL_1>` was
  reserved only within its own text, so the next turn handed the token to a real PESEL and restore
  wrote that PESEL into the template. Such a token now stands for itself in the mapping, which the
  caller already carries from turn to turn.
- **A known value right before a secret stayed in the clear** when widening it to the whole word
  would have crossed the secret's token; the value itself is masked now.
- **Masking an export of many distinct records took minutes.** Each known value was searched for in
  its own pass over the text: 1 MB holding 36,000 distinct e-mail addresses and phone numbers took
  47 s, and 5 MB over eleven minutes. They are now found together in one pass, in 5 s and 31 s,
  most of which is the guards' own work; `mask_known_values` and the output sweep gained the same.
- **The allowlist missed brands written with invisible characters**, such as "Face" + soft hyphen
  + "booku" copied from a web page, fullwidth letters or decomposed accents, and kept them masked.
- **The hub's last check before sending refused payloads that held no original value**: it found a
  denylisted word inside a token's label, "KARTA" in `<KARTA_1>`. Tokens are taken out before it
  looks, except text that is no token: a placeholder the user typed, and a value the mapping hides,
  so a password written as `<ADMIN_2024>` is still found. A short number counts exactly where the
  library would mask it, so a known PIN no longer refuses every later message with a date of that
  year. A provider error quoting such a placeholder is audited instead of breaking the turn.
- **An IBAN wrapped onto the next line between its groups was missed**, as a narrow column in a PDF
  or an e-mail wraps it. The rule now reads such a line break as a group separator.
- **A vault row's fingerprint gave the value back.** It was a plain sha256, and there are only so
  many PINs and PESELs: trying them all took seconds for a PIN and minutes for a PESEL, with no need
  for the vault key. It is now an HMAC keyed from the vault key. Rows written earlier keep their old
  fingerprint until they expire.
- **Dependency floors allowed releases with known vulnerabilities.** `pypdf>=4.3` admitted 93
  advisories against the library that reads uploaded PDFs, `python-multipart>=0.0.9` 14 against
  the one that parses every upload, `cryptography>=43` 11 and `jinja2>=3.1` 10, and FastAPI's own
  floor let pip keep a Starlette with 14. Each floor now starts at the first release without a known
  advisory, and Starlette, which the hub imports directly, is declared.
- **Polish identifiers the guards missed.** An IBAN followed by a currency, a BIC, an amount or a
  bank name ("PL61 ... 2874 PLN") went out unmasked: the pattern took the next word for one more
  group and the checksum then failed. An IBAN is now cut to its country's registered length. Also
  masked now: IBANs, compact ID card numbers and labelled VINs in lowercase; IBANs with hyphens or
  en dashes between their groups; cards next to an expiry date or another number; PESEL and NIP
  written one digit per box, or split after a PESEL label; registrations after the inflected labels
  Polish uses ("o numerze rejestracyjnym", "tablica rejestracyjna:"); addresses with a patron's
  title ("ul. gen. Andersa 15") or a flat ("lok. 4"); Cisco MAC addresses; "KRS nr ..."; and
  14-digit REGONs written in two parts.
- **Secrets in their current formats.** OpenAI project, service-account and admin keys, Stripe,
  GitHub fine-grained and GitLab tokens, Slack webhooks, Bearer and Basic authorization headers,
  passwords inside connection URLs whatever the host, PGP and encrypted private keys or keys without
  their END line, quoted passphrases with spaces, and credentials introduced in Polish ("Hasło do
  Wi-Fi:", "Kod dostępu:", "PIN"). A password at the end of a sentence no longer takes the full
  stop into its value, which had left every later mention of it in the clear.
- **Names the way Polish writes them.** A surname after initials as in a signature ("J. Kowalski"),
  surname-first order as in forms and lists ("Konieczny Jan"), a surname in capitals ("JAN
  KONIECZNY"), a man's surname left in the nominative after his vocative first name ("cześć tomaszu
  wróbel") and medical titles ("lek. med. Kowalski") now mask the whole person. The word "lek", an
  occupation after "pan" or "pani" ("z panem hydraulikiem") and a month inside a date ("15 Jan
  2024") no longer become people, and an allowlisted brand that is also a surname ("pani Ziaja") no
  longer unmasks the person.
- **Company names whole and apart.** Every common spelling of a Polish legal form ("sp z oo",
  "sp. z o.o", "SP. Z O.O.", "spółka komandytowa"), quoted names (`„ABC” Sp. z o.o.`), names with a
  number, a bracket, a dot or a preposition ("Restauracja Przy Moście"), and names of up to eight
  words are found. A name no longer takes in a heading, the previous sentence, or the signatory
  above it on separate lines, and two companies joined by "i" ("Muster GmbH i Kowalski Handel Sp. z
  o.o.") get a token each. A dotless "SA" counts after two words of a name or one in mid-sentence,
  so a court of appeal ("Wyrokiem SA w Krakowie") is not a company. Denylist terms match across
  no-break spaces, line breaks and missing Polish letters, and overlapping terms are masked as one.
- **Foreign formats.** EU VAT numbers in their national spacing; German register numbers and
  Steuernummer; the Czech IČO and the Belgian enterprise number; bank account labels in their usual
  spellings; German and US street shapes ("Berliner Straße 12", "350 5th Avenue"); postcodes under a
  street, a company or a recipient, and US ZIP codes with or without a comma; phone labels in
  French and German; names with an inner capital, an apostrophe, a hyphen or an accent ("McDonald",
  "O'Neill", "Jiří"); and companies printed in capitals. Polish prose is left alone: "Sterownik
  PLC", "Klasa AB", "Masa netto KG", a year heading, a ticket id, a hex colour, a signed amount and
  a VAT amount are no longer masked, while "Siemens AG potwierdził zamówienie" still is.
- **Ordinary numbers masked as identifiers.** Invoice and warehouse document numbers ("FV
  2024123", "WZ 2024001") are no longer passports, amounts with a currency code ("PLN 100000") no
  longer ID cards, a phone number with the 0048 prefix no longer a card, a date-prefixed order
  number ("204410-00102") no longer a PESEL without a PESEL label before it, a 17-character request
  id no longer a VIN, and "data rejestracji FV 12345" no longer a vehicle registration.
- **Regular expressions that could stall the library.** A street prefix followed by a run of "I"
  tokens, as OCR reads the rules of a table, backtracked exponentially, and a long line without an
  "@", a base64 or hex blob, or hyphen-joined tokens took quadratic time in the e-mail, company and
  foreign-format rules. All of them now run in linear time.
- **Text from PDFs, Word and macOS went out unmasked.** A no-break space or an en dash between the
  groups of an IBAN, NIP, phone or card number, a zero-width space or a direction mark inside a PESEL, and a Polish
  letter written as a base letter plus a combining accent each stopped every guard: a whole IBAN, or
  the second half of a surname, reached the provider. The guards now read the text with letters
  composed, such spaces and dashes made plain and invisible characters dropped, and every span is
  mapped back, so the mapping keeps the text exactly as written.
- **A token the text already contained was handed out again.** A template's literal "<PESEL_1>"
  next to a real PESEL got the same token, and restore turned the placeholder into the real number.
- **Token numbers did not follow reading order** when a value was recognised only at a later,
  labelled occurrence.
- **A stray piece of a losing detection was masked across the whole document.** An address rule
  that ran from an e-mail domain into the next line left the bare word "NIP" behind, and every "NIP"
  in the document reached the model as `<ADRES_1>`. Such pieces are still masked where they are, but
  no longer count as known values.
- **The judge rewrote words.** A finding was replaced at every raw occurrence, so "Kos" turned
  "Kosztorys" into "<OSOBA_1>ztorys". Findings now follow the rules known values do: a short one is
  never matched inside a longer word, a longer one takes the whole word under its own token, a
  finding that quotes or cuts a token allocates nothing, and `result.spans` records exactly what the
  judge replaced.
- **A judge reply in a Markdown fence, or after a sentence, failed every turn**, which turned stage 2
  off without a sound. A reply with no content, or of an unexpected shape, now ends in `JudgeError`
  instead of a `TypeError` that escaped the fallback policy, and a judge that raises anything else
  still gets the fallback the caller chose.
- **Masking a long document took minutes.** Every further occurrence of a known value was checked
  against all masked ranges one by one: a 640 KB text took 19 s and 1.28 MB took 101 s. The check
  is a bisection now, and an 850 KB document takes about 2 s. The work also runs in the worker
  thread with the guards instead of blocking the caller's event loop.
- **The language model guard broke on long texts and on concurrent first use.** spaCy refuses more
  than a million characters, so a long text is now cut at line breaks; and threads that need the
  model at the same moment load it once instead of once each.
- **A long custom token split across stream chunks was not restored**, and the output sweep reported
  the conversation's own values as new personal data when a guard read them with a trailing full
  stop, read part of one, or read two of them as one name.
- **The hub never started from anything but a source checkout.** The wheel carried the Python code
  and none of the templates, static files or translations, because no package data was declared.
  Every editable install reads them from the source tree, which hid it; the Docker image and any
  built wheel would have failed on import, looking for `icons.html`. A new CI job installs the
  built wheel away from the checkout and serves a page from it.
- **The name of an uploaded file was stored in the clear.** The extracted text was encrypted, while
  "CV Jan Kowalski.pdf" sat beside it in plaintext, often the most personal string in the whole
  upload. The `filename` column now holds the masked name, as the provider saw it, and the original
  is encrypted in a new `filename_enc` column and decrypted only for its owner's page. The column is
  added at startup; a file uploaded before the upgrade keeps its name as it was until retention
  removes it.
- **Masking a word that had already gone out blocked the conversation for good.** Earlier turns are
  replayed from what was sent at the time, so once "Projekt Atlas" was typed into the "also mask"
  box, or added to the denylist, or masking was switched back on, the final check found the raw
  value in the replay and refused that turn and every later one, with nothing an employee could do
  but start over. Replayed history and attachments are now masked with the conversation's current
  mapping before the check, by the rules `anonymize()` already applies to known values, which the
  library now exposes as `mask_known_values()`. Stored messages are left as they were, because they
  are the record of what the provider received.
- **A token reused after its mapping expired was never stored.** `load_mapping` skips an expired
  row, so the allocator starts its numbering again and hands `<OSOBA_1>` to the next person it
  meets, while `save_mapping` counted that same expired row as already stored and dropped the new
  value. The purge runs hourly, so every installation with a vault TTL had an hour a day in which a
  token could go to the provider with nothing left to restore it from. Both sides now decide
  expiry the same way.
- **A one-column CSV saved by Excel stopped the upload.** A single column leaves nothing to sniff,
  so on CRLF input `csv.Sniffer` reported the carriage return that repeats on every line,
  `csv.reader` refused to read with it, and the `ValueError` escaped a handler that caught only
  `csv.Error`. A sniffed dialect whose delimiter is a line ending is now discarded for the Excel
  one. The bug was never specific to any platform; it surfaced when the suite ran on Windows,
  where the test wrote its own sample file with CRLF.
- **A Polish VAT number printed with its "PL" was never recognised.** The NIP pattern accepted the
  prefix and the checksum validator then rejected "PL8992611767" for not being ten digits, so the
  way a Polish company writes its number on an EU invoice was left to whatever else happened to
  notice it.
- **A word masked by mistake stopped the whole document.** Five of the owner's twenty-four sample
  invoices never reached the provider: the model took "Television" for a company, the product code
  "TelevisionM12037" in the same line still contained it, and the final check refused the message.
  A known value inside a longer word now masks the whole word, so "Kowalskiego" becomes a token of
  its own as well, and a value that ends in punctuation ("DD." in "DD.MM.YYYY", which stopped the
  Swiss sample) no longer counts as inside a word at all. Values shorter than four characters are
  still left to the final check, because widening "Jan" to every word that contains it would mask
  half a document.
- **The answer never appeared while it streamed.** The page listened for an event the HTMX SSE
  extension only fires for `sse-swap` targets, so the bubble stayed empty until a reload. The stream
  is now read with `EventSource`, which is closed on the last event instead of reconnecting to a
  turn already taken. While the message is masked and while the model thinks, the bubble says so.
- Times were shown in UTC, two hours behind a reader in Warsaw; the page shows local time now.
- The composer grew a scrollbar with every line, the empty-conversation illustration stayed under
  the first message sent, and HTMX logged a Content-Security-Policy violation on every page load.
- **A name typed in lowercase reached the provider as typed.** "Z panem kamilem kowalczykiem"
  left the hub unmasked, because the only detector for people was a model that relies on capital
  letters. `PersonNameGuard` masks it now, with the model or without it.
- `merge_spans` could unmask part of what a guard flagged. Of two overlapping detections it kept
  only the more confident one, so a name span around an e-mail address lost the name, and a
  shorter, more confident detection of a name left the rest of it in the clear. Overlaps of one type
  now become their union, and a detection that loses keeps what it covered outside the winner.
- The Polish model tagged lowercase adjectives ("polskiej", "angielskiej") as places; a single
  lowercase adjective is no longer masked as a location.
- Business abbreviations the model took for companies — SEO, PPC, CRM, UX and the like — are no
  longer masked: "co to jest SEO?" went out as "co to jest <FIRMA_1>?".
- A value detected once was left unmasked wherever else it appeared: on a real invoice the billing
  postcode was masked and the delivery postcode two lines below was not. Every standalone
  occurrence of a known value now gets its token.
- A turn the final check refused raised out of the SSE stream after the message had already been
  saved, with the caught value in the plaintext `content_sent` column. That message was replayed as
  history, so every later turn was refused too. The check now runs before anything is stored.
- `TOTAL M0061 Catalyst 1` was read as an address (`al.` inside `TOTAL`), cutting a token into the
  middle of the word on every invoice with a total line.
- Tokens are allocated in reading order and substituted right to left, so the first person in a text
  is `<OSOBA_1>` rather than `<OSOBA_2>`.
- `merge_spans` ranks by confidence before length, so a loose model span no longer swallows the
  checksum-validated identifier sitting inside it.
- The provider was sent the raw file name alongside the masked text; "CV Jan Kowalski.pdf" would
  have arrived unmasked.
- `?next=` accepted absolute URLs, which made the login page an open redirect. It also refuses
  control characters now, since a browser drops the tab in `/<tab>/evil.example` and goes off-site.
- `highlight_tokens` escaped the text before matching, so the product's signature highlight silently
  never rendered.
- Credentials named with a prefix (`DB_PASSWORD=`) were not matched, because `\b` treats the
  underscore as part of the same word.
- Uploads were read into memory before their size was checked.
- The chat page returned 500 for any conversation holding a file that was waiting for review or had
  been rejected: the loop bound `f` while the partial it included read `file`.
- Queued turns were held in a module-level dictionary, which lost messages on restart and broke with
  more than one worker.

### Security

- **Whatever was typed as a username was kept in the audit trail.** A failed sign-in recorded the
  name in clear and shown on the audit page, and what arrives in that field is often a password
  typed into the wrong box or a PESEL; an anonymous client could also store a megabyte at a time. A
  name that matches no account is now recorded only as its SHA-256, which still counts attempts at
  that name towards its lockout.
- **An administrator's session could set a new password without the current one.** The reset in
  the user list worked on the administrator's own account too, with no current password and no
  lockout, so a stolen administrator session could take the account over and sign its owner out.
  An administrator's own password now changes only on the account page, like anyone else's.
- **A file named .pdf that was not one put its first bytes in the log.** pypdf warns in its own
  logger with the bytes it found in place of a PDF header, and the start of a document is often a
  name. A file without the header is now refused before pypdf sees it, and pypdf's warnings are
  kept out of the log; its errors still appear.
- **A small Office file could exhaust the hub's memory.** DOCX and XLSX files are ZIP archives of
  XML, and repetitive XML compresses a few hundred times. python-docx parses a whole document, and
  openpyxl a workbook's whole table of shared strings, before the first paragraph or row is read,
  at about eight and three times the XML's size, so a 3.4 MB document or a 10 MB workbook under the
  upload limit would have needed 8 to 11 GB. Stopping at the character limit could not help, since
  the cost is paid before reading starts. The sizes the archive declares are now checked first:
  more than 64 MB of XML that is parsed whole, or 200 MB in all, and the file is refused unread with
  a reason the employee can act on. A real 10 MB export of 400,000 rows, 105 MB unpacked and
  streamed, is still read.
- **The no-inline-code test missed most of the forms it exists to catch.** It matched patterns, so
  an unquoted `onclick=`, `ONCLICK`, `<SCRIPT>`, `data-hx-on`, `hx-vals="js:..."` and `javascript:`
  URLs all passed it. Templates are parsed now, with Jinja blocks set aside so a `>` in
  `{% if count > 1 %}` cannot end a tag early, and every template, partials included, is also
  rendered and checked; the check is tested against each of those forms and the other htmx
  attributes that evaluate `js:`. htmx is also told never to evaluate strings or run scripts in
  swapped content, should the policy ever be loosened.
- **The sign-in lockout could be outrun, and the account page bypassed it.** The failure count was
  checked before the password and written after it, so every request of a parallel burst passed the
  check: 16 of 30 simultaneous guesses were tried against a limit of 5. An attempt is now recorded
  before its password is checked, and only the first five of a window are. The current password on
  the account page answered wrong or right without limit to anybody holding the session; it counts
  towards the same lockout now. An unknown or disabled account was refused six times faster than an
  active one, which told an outsider which usernames exist; argon2 runs for all three, against a
  hash prepared at startup so even the first unknown name takes as long.
- **A session outlived a new password.** The session cookie carried only a signed user id, so after
  a password change, an administrator's reset or signing out, a copied or stolen cookie kept
  working for the rest of its 14 days, and one opened with the generated admin password survived
  the change the README asks for. Accounts now have a session epoch, carried in the cookie and
  raised by each of those steps and by disabling or enabling the account; a cookie from an earlier
  epoch is refused. The browser that changed its own password stays signed in. Signing out ends
  the account's sessions everywhere, and an upgraded installation signs everybody out once. The
  epoch is raised by the database, so two sign-outs at once both count, and a sign-in whose
  password changes while it is being checked gets a cookie that is already dead.
- **A link on another site could act in the hub.** Creating a conversation and signing out were
  plain links, and `SameSite=Lax` cookies travel with a link followed from another site; the answer
  stream, which sends the queued message to the provider, is a GET as well. All three escaped the
  check that refuses cross-site requests. Creating a conversation and signing out are forms now,
  and the old link just opens the chat. The stream answers only an `EventSource`: over plain HTTP a
  browser sends no Fetch Metadata and no `Origin` on a link it follows, so the stream also requires
  the `Accept: text/event-stream` that a followed link cannot send. Opening the chat for the first
  time still creates the conversation it opens, which a link from another site can therefore do
  once; it holds nothing.
- **Some answers went out without the hardening headers.** The refusal of a cross-site request and
  the page for an unhandled error were built outside the middleware that adds the
  Content-Security-Policy, `nosniff`, `DENY`, `no-referrer` and `no-store`, and the error page was
  an untranslated plain-text line. Both carry the headers now, and an unhandled error gets the
  translated error page, or one line in an HTMX swap. An administrator's form posted with a role, an
  approval mode or a judge fallback it never offers is refused with a 400 instead of failing with a
  server error.
- **A sibling subdomain could act as the administrator.** The origin check accepted
  `Sec-Fetch-Site: same-site`, and `SameSite=Lax` cookies travel with such requests, so a page on
  any other subdomain of the company's domain (an intranet tool with an injected script, a dangling
  subdomain somebody took over) could create an administrator account or switch the masking off.
  Only same-origin requests and ones the user started from the address bar are accepted now.
- **A rejected file could be attached after all.** Approving did not look at the file's status, so a
  review page left open in another tab, or reached with the back button, attached a document the
  employee had rejected and sent its masked text with the next turn. Rejecting also kept the
  extracted text, encrypted original included, for as long as history is kept. Approving a rejected
  file is now refused, rejecting drops the extracted text and the original file name (the masked
  name stays, so the conversation still shows a file was turned down), and the review page offers
  its buttons only while the file waits for a decision.
- **A failed upload left the document on disk.** A damaged DOCX, XLSX or PDF, a judge outage under
  `judge_fallback="block"`, or any other failure after the bytes were written ended in an
  untranslated server error and left the original file in the upload folder with no row pointing
  to it, out of the retention job's reach. A failed upload now removes its bytes and tells the
  employee why: a file that cannot be read, or a judge that is down. A file of a type the hub cannot
  read is refused before it is written, so the stored name no longer carries the tail of the
  employee's file name either; a long tail also failed the upload with that part of the name quoted
  in the log. The hourly retention pass removes stored uploads that no row refers to once they are
  an hour old. An upload whose browser went away mid-read is removed as soon as the reading thread
  lets go of it: Windows refuses to delete a file that is still open, and the failed delete used to
  replace the cancellation with an error of its own.
- **A judge failure could put personal data and secrets in the log.** An unreadable reply was quoted
  in the `JudgeError` message, and the reply consists of the fragments the judge found identifying;
  a remote judge's transport error quoted the request header, API key included. Errors now give a
  length, an HTTP status or an exception type, and the API key is stripped of the newline a key file
  ends with.
- **`judge_fallback="block"` let a failing judge through.** Given as text it compared unequal to the
  enum, so a judge outage meant sending stage 1 alone instead of refusing.
- **Safe with nothing configured.** The session key defaulted to `dev-only-change-me` and the
  example file shipped `change-me-to-a-long-random-string`: a hub started with either signed its
  cookies with a key anybody can read here, so anybody could forge an administrator's session and
  read the decrypted history. The first admin defaulted to `admin`/`admin`. Now an empty or
  placeholder `SECRET_KEY` makes the hub generate one and keep it beside the database, and an empty
  `ADMIN_PASSWORD` gets a generated password written to `initial-admin-password` there, readable by
  the service account only; the log names the file, never the password. The vault key already
  worked this way and now shares the code. An installation that relied on a placeholder key signs
  everybody out once after the upgrade.
- **The Docker image keeps its state in `/data`.** Without `DATABASE_URL` the database landed
  beside the installed package, which the image's service account cannot write to; only the compose
  file set it. `docker run` with no environment now starts.
- The extra terms an employee types into "also mask these words" were queued in a plaintext column
  while the message waited for its stream. They are, by construction, the values that person
  considered most sensitive; they are encrypted now, like the message beside them.
- The audit row for a sent message records which stage-2 backend ran (`judge=mock`, `judge=ollama`,
  `judge=remote`, `judge=off`), so "did masked text leave the network for the judge as well?" can be
  answered from the log rather than from memory.
