# Leak evaluation

The tests check one case each. This evaluation answers the question they cannot: in conversations
written the way people write them, how much personal data reaches the provider?

[`conversations.json`](conversations.json) holds 27 made-up conversations, 60 turns in all: payroll
and rental files attached, a CV whose file name is the candidate's name, an e-mail pasted with its
signature, a surname in another case two messages later, a PESEL typed wrong, an English reply, and
two conversations with no personal data at all. Each one lists the values that must not reach the
provider (`must_not_leak`, 125 in all) and ordinary phrases that must reach it as written
(`must_survive`, 76), such as "Lista płac" next to the surname Lis.

[`run.py`](run.py) plays every conversation through the running hub the way a browser does: it
signs in, uploads and approves the attachments, sends each turn and reads the answer stream. Masking,
the check before every provider call and restoring the answer are the hub's own code, unchanged. The
provider is a small HTTP server on localhost that records every request and answers by repeating the
masked message back in three-character pieces, so tokens arrive split and the restored answer has to
equal what the employee typed. No paid API is called and nothing leaves the machine.

It counts:

- **leaked**: a listed value found in anything the provider received, as a word of its own; **in
  part** when only a piece of it was, such as the surname of a full name or the login of an e-mail;
- **masked anyway**: a listed phrase that did not reach the provider as written;
- **refused**: turns the hub would not send because its last check found an original value;
- **restored exactly**: answers that came back word for word as the message was typed;
- **masked off the list**: values masked that no list names, shown with `--off-list`. Not an
  error by itself: it is where a company name or a city on its own turns up.

## Running it

From the repository root, with the hub installed (`pip install -e .`) and the spaCy model
downloaded:

```bash
python evals/run.py                         # mock judge, about a minute
JUDGE_BACKEND=ollama python evals/run.py    # the local Ollama judge, if one is running
python evals/run.py --check                 # exit 1 on a finding known_gaps.json does not list
python evals/run.py --off-list --json out.json
```

CI runs `--check` with the mock judge after the tests. [`known_gaps.json`](known_gaps.json) lists
the leaks and over-masking polanon is known to produce here, none at the moment, so the check fails
on anything new, a refusal, an answer restored wrong, or a listed gap that has gone away and should
come off the list.

## Results

Mock judge, the hub at version 0.1.0 plus the changes on `main`:

| | polanon 0.2.2 to 0.3.2, 11 November 2026 | polanon 0.3.3, 12 November 2026 |
|---|---|---|
| values that must not leak | 125 | 125 |
| leaked whole | **4** (3.2%) | **0** |
| leaked in part | 0 | 0 |
| phrases that must reach the provider | 76 | 76 |
| masked anyway | **2** | **0** |
| turns refused before sending | 0 | 0 |
| answers restored exactly | 60 of 60 | 60 of 60 |
| values masked off the list | 4 | 2 |

The first run found four leaks, all misses of polanon's detection rather than of the hub, which can
only check for values polanon masked. polanon 0.3.3 fixed each of them, with a test, and the hub
requires it:

| Conversation | Value | Why |
|---|---|---|
| `it-support` | a password after "Hasło administratora:" | not a label polanon knows; "Hasło:" and "Hasło do serwera:" are |
| `pesel-correction` | a PESEL typed wrong, after "PESEL:" | its checksum fails, and the label alone is not enough |
| `pesel-correction` | "Bielskiej", a turn after "Natalia Bielska" | a feminine surname in another case, with no "pani" before it |
| `courier-pickup` | "Długiej 17" | a street with neither "ul." nor a postcode |

It also masked "Płatnik", the name of ZUS's payroll program, and "ZUS" itself, both taken for
company names; 0.3.3 put both on its list of well-known names. What is still masked off the list is
a company name and a city on its own, which the lists leave out on purpose.

Having found no leak in conversations its author wrote says little about conversations nobody has
written yet. Each new miss belongs in `conversations.json`, and in `known_gaps.json` until polanon
fixes it.

The local Ollama judge was not measured: Ollama is not installed on the machine these numbers were
taken on. With `JUDGE_BACKEND=ollama` and nothing listening, on polanon 0.2.2, the report counted
60 judge failures in 60 turns: the hub falls back to stage 1 on every one, as its default policy
says, and the other numbers were those of the mock judge.

## What these numbers do not show

- **The author wrote the conversations.** The hub's author wrote them and knows polanon's rules, so
  they are not a sample of real traffic, and 27 conversations are few. They are mostly Polish, with
  one in English. Read the numbers as a regression check that says how the hub behaves on these
  conversations, not as a leak rate to expect in a company.
- **The lists are a judgement too.** Company names, company addresses and a city on its own are not
  on them. Identification by context ("the director of the only steelworks in town") is not on them
  either, so stage 2 is not measured: the mock judge only knows the two phrases of the demo.
- **The provider is an echo.** A real model rewrites the message and inflects the names it puts back
  ("Jana Kowalskiego" for a token that stands for "Jan Kowalski"). Restoring here measures the hub's
  tokens coming back, not the grammar of a model's answer.
- **A leak is counted as written.** A value is looked for as a word of its own in the form it has in
  the conversation; the same value in another spelling reaching the provider would not be counted.

## Adding a conversation

Add an entry to `conversations.json` with a unique `id`, its `turns`, any `attachments` (`.txt` or
`.csv`, given as `filename` and `text`), and the two lists. `run.py` refuses a listed value or phrase
that does not occur in its conversation. Identifiers must belong to nobody: a PESEL born before 1860
or after 2100 with a valid checksum, a test card number such as 4111 1111 1111 1111, a reserved
e-mail domain such as `example.com` or `.example`.
