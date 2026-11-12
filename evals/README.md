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

The same server answers `/embeddings` with a stand-in model ([`bag_of_words.py`](bag_of_words.py)),
and the attachment budget is set to 120 characters, so every attachment goes through the search in
attachments: its passages, and each question about it, go to the embeddings endpoint, and whatever
reached that endpoint counts as received, exactly like the chat. `--no-search` plays the
conversations with the search off, the way the hub runs until `EMBEDDING_MODEL` is set.

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
python evals/run.py --no-search             # attachments cut to the budget, no embeddings
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

With the search in attachments, on 13 November 2026 with polanon 0.3.3, the numbers are the same:
0 of 125 values leaked, whole or in part, 0 phrases masked anyway, no turn refused and 60 of 60
answers restored exactly. The check covered 15 requests to the embeddings endpoint with 28 texts,
passages of the four attachments and the questions about them, as well as the chat.

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

## Search in attachments: does the answer reach the model?

A file longer than the attachment budget used to be cut to its first characters on every turn.
[`retrieval.json`](retrieval.json) holds four long documents (work rules, an office lease, an IT
security policy and a quarterly sales report), masked the way the hub stores an attachment, and 16
questions whose answer lies past the budget. [`retrieval.py`](retrieval.py) cuts each document into
passages and ranks them for each question with the hub's own code, in three ways: by words in
common, by vectors, and by both, as the hub does. It counts how often the passage holding the
answer comes first (hit@1) or among the first three (hit@3), and whether the answer is in the text
a turn would send: the best `top_k` passages that fit the budget. The file cut to the budget is
scored on that last count alone.

13 November 2026, budget 1 500 characters, passages of 400, top_k 6, stand-in vectors:

| | hit@1 | hit@3 | reaches the model |
|---|---|---|---|
| cut to the budget, as before | | | **0 of 16** |
| search: words only | 14 of 16 | 15 of 16 | 15 of 16 |
| search: vectors only | 9 of 16 | 13 of 16 | 15 of 16 |
| search: both, as the hub ranks | 16 of 16 | 16 of 16 | **16 of 16** |

The vector row says little: the stand-in model counts words without telling rare ones from common
ones, which a real embedding model does far better. The two ways fail on different questions,
which is why the hub adds them. Words alone never find "Jak długo trzymane są backupy?", since the
document says "kopie zapasowe", while the vectors put that passage first; vectors alone miss "Gdzie
i kiedy można zastać `<OSOBA_3>`?", while words find it through the token, matched exactly. A real
model should do better on the first kind of question, which is what the LM Studio run below is for.

### Measuring a real embedding model with LM Studio

1. In LM Studio, download `bge-m3` (a multilingual model that reads Polish well) and load it.
2. Start the local server (Developer, Start Server); it listens on `http://localhost:1234` by
   default. Note the model's name in the list of loaded models, for example `text-embedding-bge-m3`.
3. From the repository root:

   ```bash
   python evals/retrieval.py --embeddings-url http://localhost:1234/v1 --embeddings-model text-embedding-bge-m3
   ```

Nothing leaves the machine: LM Studio computes the vectors locally. The same two settings in
`.env`, `EMBEDDING_BASE_URL=http://localhost:1234/v1` and `EMBEDDING_MODEL=text-embedding-bge-m3`,
make the hub itself use it.

### What these numbers do not show

- **The author wrote the documents and the questions**, and placed every answer past the budget on
  purpose, so the cut file scores nothing by construction. Sixteen questions are few.
- **The documents are masked already.** They are written with tokens in place of names, the way the
  hub stores them, rather than masked by polanon on the way in.
- **The budget is scaled down.** The hub's default is 60 000 characters with passages of 1 200;
  here it is 1 500 with passages of 400, so that a short document is long enough to need the search.
  The ratio of passages sent to passages in the file is close, but a real file of hundreds of pages
  has many more passages to choose from.
- **Reaching the model is not answering.** The count stops at the text the model receives; whether a
  given model then answers correctly is not measured.

## Adding a conversation

Add an entry to `conversations.json` with a unique `id`, its `turns`, any `attachments` (`.txt` or
`.csv`, given as `filename` and `text`), and the two lists. `run.py` refuses a listed value or phrase
that does not occur in its conversation. Identifiers must belong to nobody: a PESEL born before 1860
or after 2100 with a valid checksum, a test card number such as 4111 1111 1111 1111, a reserved
e-mail domain such as `example.com` or `.example`.
