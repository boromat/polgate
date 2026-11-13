# Threat model

This document states plainly what the hub protects against and what it does not. Read it before
relying on the project for anything that matters. It is engineering documentation, not legal advice.

## What the hub is for

An employee wants to use a capable cloud model on work material that contains personal data. Sending
that material directly to the provider would disclose it. The hub sits in between, replaces personal
data with placeholder tokens locally, sends only tokens, and puts the originals back into the answer.

The masking itself is done by the library [polanon](https://github.com/boromat/polanon), which the
hub installs from PyPI. Its own
[threat model](https://github.com/boromat/polanon/blob/main/docs/THREAT_MODEL.md) covers the library
used on its own; this one covers the hub, the library's detection limits included.

## Trust boundaries

| Zone | Contents | Trusted? |
|---|---|---|
| Employee browser | original text as typed, restored answers | yes, it is the person who owns the data |
| Hub host | database, vault key, uploaded files, local models | yes, this is the company's own machine |
| LLM provider | only tokenized text | **no**, this is the boundary the project exists for |
| Stage-2 judge on Ollama | stage-1 masked text, stays on the hub host | yes, it never leaves the network |
| Stage-2 judge on `remote` | stage-1 masked text sent to the cloud | **no**, testing aid only |
| Embeddings endpoint on the hub host (LM Studio, Ollama) | masked passages of long attachments and masked questions | yes, it never leaves the network |
| Embeddings endpoint elsewhere (the chat provider, a cloud API) | the same, sent out | **no**, the same boundary as the LLM provider |

## What is protected

- **Deterministic identifiers.** PESEL, NIP, REGON, ID card, IBAN and payment cards are matched by
  pattern and confirmed by checksum, and KRS numbers by the label next to them, so detection is
  near-certain and false positives are rare.
- **Contact details.** Email addresses, phone numbers, postcodes and IP addresses.
- **Credentials.** API keys, JWTs, private key blocks and `password=` style assignments, so pasted logs
  and configuration files do not leak secrets.
- **Company names.** `CompanyGuard` finds a name by its Polish legal form (`Sp. z o.o.`, `S.A.`,
  `sp. j.`, `s.c.`), which is a rule rather than a guess, and the admin denylist catches the rest by
  a case-insensitive match that also tolerates missing Polish letters, line breaks and no-break
  spaces. A company with neither a legal form in its name nor an entry on the denylist depends on
  the model.
- **The outbound path itself.** `assert_no_raw_values` runs before every chat request to the provider
  and before every request to the embeddings endpoint, passages and question alike (the `remote`
  stage-2 judge, a testing aid, sends stage-1 text without it), and raises if any
  known original value appears in the payload letter for letter, outside the conversation's tokens.
  A short number such as a PIN counts only where the library masks it, which leaves a date that holds
  its digits alone (see below). The check is independent of how the library masked, so it catches
  a value the masking missed in its exact spelling; other spellings of a value rely on the library.
  This is enforced in code and covered by tests; it does not depend on the model following
  instructions.
- **Data at rest.** Originals in message history, file text and the vault are encrypted with Fernet and
  expire according to the retention policy. A vault row's fingerprint is an HMAC keyed from the vault
  key, so it does not give a PIN or a PESEL back to someone who holds only the database. The
  retention job really deletes: mappings, messages, uploaded files with their bytes on disk,
  conversations left empty, queued turns nobody streamed, and stored uploads no row refers to. A
  failed upload removes its own bytes at once.
- **Passages and vectors of the search in attachments are stored in plaintext, on purpose.** A
  passage is a slice of a file's `text_sent`, which is masked and stored in plaintext already, and
  its vector is computed from that slice. Inverting an embedding can recover an approximation of the
  text it came from, but here that text is masked and sits in the same row, so encrypting the
  vector would hide nothing an attacker with the database does not already read, and would cost a
  decryption per passage on every turn. **The caveat:** this holds only while `text_sent` is stored
  in plaintext and only masked text is embedded. If either changes, the vectors must be encrypted
  with it, because a vector of an original text gives that text back in part.
- **A passage whose token has expired is not sent.** When a vault row expires before the file, its
  token's number can be given to another value; a passage still holding it is skipped by the
  search, and counted in the audit row, so an answer is not restored with somebody else's name.
  A file short enough to be sent whole is not filtered this way, as before the search existed.
- **The employee's browser session.** The Content-Security-Policy admits scripts and styles only from
  the hub's own origin (they are all served from `/static`) and allows neither `unsafe-inline` nor
  `unsafe-eval`, so an injected `<script>` does not run; there is no inline script or handler in any
  template for it to hide among, and a test enforces that. Session cookies are `HttpOnly`,
  `SameSite=Lax` and `Secure` when `COOKIE_SECURE` is on, and a state-changing request whose
  `Origin` or `Sec-Fetch-Site` says it came from another origin is refused, a sibling subdomain of
  the same company included. A new password, an administrator's reset, disabling or enabling the
  account and signing out each end every session the account has, so a stolen cookie stops working
  once the usual response to a compromise has been taken; signing out in one browser therefore signs
  the account out in all of them.
  The page is served `no-store`, so a shared workstation does not keep it in the back/forward cache.
- **Guessing a password.** argon2id, plus a lockout after `LOGIN_MAX_ATTEMPTS` failures counted from the
  audit trail, which holds across workers and survives a restart. An attempt is recorded before its
  password is checked, so a burst of parallel requests gets no more guesses than the limit, and the
  current password on the account page counts towards the same lockout. Sign-in runs argon2 for an
  unknown or disabled account too, so the time an answer takes does not tell which usernames exist.
  Sign-in refuses to redirect anywhere outside the hub, so the login page cannot be borrowed for the
  first hop of a phishing flow.
- **The audit trail.** Records entity types, counts and hashes. `audit.log` raises if a caller tries to
  put a known original value into a detail field.

## What is not protected

- **Names and places are detected partly by rule, partly statistically.** `PersonNameGuard` knows
  the common Polish first names in every case, in any letter case and without Polish letters, and the
  surname or form of address next to them. Everything else — a surname on its own in lowercase
  ("napisz do kowalskiego"), a rare or foreign first name, every place name — depends on the spaCy
  model, which leans on capital letters and misses what people type in lowercase. A miss is a
  detection gap, not a bypass, but the data still reaches the provider. The stage-2 judge exists to
  narrow this gap, not to close it.
- **A one-word name masked once and written later in lowercase.** Once a value has a token, every
  later occurrence is masked however it is spelled: in capitals, across a line break, with a no-break
  space or an invisible character in it. A capitalised one-word name in all lowercase is the
  exception, because many Polish surnames are also ordinary words (Zima, Kot, Wilk), so "kowalska
  dzwoniła" after "Pani Kowalska" goes out unless a detector recognises it on its own. Once one
  does, the lowercase spelling has a token of its own and is masked from then on.
- **A short number that is part of a longer one.** A PIN or another number under six digits is not
  masked inside a date, an amount or a longer number ("15.04.2025" when the PIN is 2025), so the
  model does not learn which numbers look like it; where it stands alone it is masked.
- **Foreign documents are read by their labels and formats.** Tax and company numbers, bank
  accounts, phones, addresses and postcodes are found where an invoice prints them next to a label
  or in an official shape, and names only after a form of address, a label or a closing formula. A
  name in running English or German prose, a city, and any format the rules have never seen depend
  on the Polish language model, which reads those languages poorly.
- **Identification by context.** "The finance director of the only steelworks in Stalowa Wola" names a
  person without naming them. The judge is asked to catch exactly this, but a small local model will not
  catch all of it. Out of the box no model reviews anything: the default `mock` judge is a demo stub
  that flags two fixed phrases, and a real review needs `JUDGE_BACKEND=ollama` (or the testing-only
  `remote`).
- **Polish inflection of tokens.** A model may write `<OSOBA_1>a` or `<OSOBA_1>owi`. Restoration matches
  tokens exactly, so the suffix survives and the sentence reads oddly. It does not leak anything.
- **Images and scans without OCR.** If tesseract is not installed, image uploads are rejected rather than
  silently sent. With OCR enabled, recognition errors become detection gaps.
- **More of a long attachment goes to the embeddings endpoint.** With the search on, the whole
  masked text of a file over the budget is sent for embedding once, passage by passage, where before
  only its first `ATTACHMENT_BUDGET_CHARS` reached the provider. It is masked and checked like
  everything else, but more masked text is more context for somebody who wants to work out who it
  is about. With a local endpoint it stays on the server; with a remote one, that endpoint is a
  provider like the chat's, and the embeddings settings are what make it one.
- **File metadata.** Only extracted text is anonymized. Author fields, revision history and EXIF data in
  the stored original are not scrubbed; the original file never leaves the hub, but it stays on disk.
- **A compromised hub host.** The vault key, the database and the uploads live there. Anyone with root on
  that machine has everything. Encryption at rest protects a stolen disk or a leaked backup, nothing more.
- **The provider's own logging.** Tokens and the surrounding text are still processed and probably logged
  by the provider. The hub reduces what is disclosed; it does not make the conversation private.
- **Traffic analysis.** Message sizes, timing and volume are visible to the provider.
- **Malicious employees.** Anyone who can type into the chat can also read their own data. The hub is a
  guardrail against accidental disclosure, not an insider-threat control.
- **Locking a colleague out.** The sign-in lockout is per account, so somebody who knows a username can
  spend five wrong passwords and keep that person out for the lockout window. That is the usual price of
  a lockout, bounded here at fifteen minutes by default; raise `LOGIN_MAX_ATTEMPTS` or shorten
  `LOGIN_LOCKOUT_MINUTES` if the trade is wrong for your office.
- **Attempted usernames in the audit trail.** A failed sign-in records the account it was aimed at, which
  is what makes the lockout and any later investigation possible. A name that matches no account is
  recorded only as its SHA-256, since what arrives in that field is often a password typed into the
  wrong box or a PESEL; attempts at the same unknown name are still counted together.
- **Denial of service.** There is no rate limit on chat, uploads or the provider budget. One employee can
  spend the whole API quota. A single upload is bounded: 10 MB on the wire, 200,000 characters of
  text, and for DOCX and XLSX the size the archive unpacks to, so a compressed file cannot make the
  hub hold gigabytes, and for XLSX the cells walked through, two million, so a sheet that claims
  every row and column cannot keep a worker busy for minutes; extraction runs in a thread, off the
  requests of everybody else.
- **An administrator who switches masking off.** Clearing every entity type in the panel turns the hub
  into a plain proxy. That is a supported setting, so nothing prevents it; the interface refuses to
  keep claiming protection afterwards and the audit row says `masking_disabled`, but a company whose
  admin does this by accident and ignores three warnings is not protected by anything here.
- **An allowlist that is too generous.** A name on the "never mask" list reaches the provider whenever
  a model or the judge flags it as a name. The default list holds public platforms and tools, and the
  list cannot release an identifier, an e-mail address, a denylisted word or a value a conversation
  has already masked; but an administrator who adds a client's name has told the hub to send it. The
  change is recorded as `policy_changed` with the field name.

## Residual risk to accept consciously

Detection is imperfect, so the correct mental model is "much less data leaves, not zero". Treat the
preview panel and the audit log as the record of what was actually disclosed, and keep the approval
policy switched on for files, where a single upload can carry hundreds of records.

## Reporting

See `SECURITY.md`. A way to get an original value into a provider payload, into the logs or the audit
table, or into another user's browser, is a security bug and we want to hear about it privately.
