# Your AI Memory vs cognee — pros, cons, and what to do next

**Written:** 2026-09-18 · **For:** planning your next steps.

---

## How I made this, and one warning

Everything about **your system** here I measured myself. I ran it, tested it, and counted.

Everything about **cognee** comes from reading their public website and code page on 18 September
2026. **I did not install or run cognee.**

That matters. Documentation tells you what software is *meant* to do. Running it tells you what it
*actually* does. Your own project proved this: several parts looked fine in writing and turned out to
be broken when measured.

Where their documents do not mention something, I say "not documented" — not "it cannot do it". One
page I tried returned an error, so I honestly do not know about that part.

---

## The short version

They are not really the same kind of tool.

**cognee** is a toolkit for developers building memory into their own apps. It handles many kinds of
data, works with many databases, and has a paid cloud version.

**Yours** answers one question very well: *what do I know, and where did it come from?*

**cognee is far more mature. Your design is sharper for your own use.** Do not rewrite yours onto
theirs. Do borrow three of their ideas.

---

## PART 1 — Your strengths

### Strength 1: You can always prove where a fact came from

**What you have.** Every fact records the file, a **fingerprint of that file's exact contents**, the
version, your machine, which AI read it, and which run produced it. All 1,245 facts have all of it.
None are missing.

**Why it matters.** The fingerprint is the special part. Even if you later edit the file, you can
still prove the fact came from a specific version of it. Without a fingerprint, "where did this come
from" is just a filename, which may have changed.

**How cognee compares.** Their documents say they track provenance. I found nothing saying they
record a content fingerprint or which AI model produced each fact.

**What to do:** protect this. It is the hardest thing to add later, because adding it means
re-processing everything. Do not let a future change weaken it.

---

### Strength 2: Your system knows when a fact was true

**What you have.** Facts have a start date and an end date. When something changes, the old fact is
closed, not deleted. You can ask what was true at a past date.

**Why it matters.** "We use Python" and "we used Python until March" are different statements. A
memory that cannot tell them apart will confidently give you out-of-date answers.

**How cognee compares.** I could not confirm this either way. Their page about time returned an
error, and their main page does not mention it. They have a "forget" feature, but deleting is not the
same as keeping history.

**Honest warning about your own version.** The design is good but the **data is currently partly
wrong**: 60 facts are marked as ended when they are still true, and 32 point the wrong way. Fix this
before relying on it. It is item 1 in `next-steps.md`.

---

### Strength 3: You can see what your AI usage costs

**What you have.** As of today, every AI call records tokens in, tokens out, how long it took, how
many retries, which model, and the exact price. Your dashboard shows cost per document and cost per
fact.

**Why it matters.** For most of this build you could not answer "what did that cost". When we worked
it out afterwards, **23% of your spending had produced nothing** — paid for, then thrown away by
bugs. You cannot fix waste you cannot see.

**How cognee compares.** Their documents mention no cost or token tracking at all.

**What to do:** put your real AWS price into `config/model-rates.yaml`. Right now it holds a
placeholder.

---

### Strength 4: You measured quality on your own documents

**What you have.** 25 test questions with known answers, written against your own vault. Your score:
**67%** of questions return the right document in the top five.

**Why it matters.** You know your real number. Most people running a memory system cannot tell you
theirs.

**How cognee compares.** They publish scores from a standard test using made-up conversations.

Neither is "better" — they answer different questions. Theirs says *how does it compare to other
tools*. Yours says *does it work on my files*. For a personal system, yours is the one that counts.

---

### Strength 5: Claude cannot damage your memory

**What you have.** Claude can read freely. To write, **two separate switches** must be on, each write
must be confirmed, there is a limit of 10 per minute and 8,000 characters, and **every attempt is
recorded — including refused ones**. Writes only add. Nothing is ever changed or deleted.

**How cognee compares.** Not documented.

---

### Strength 6: You wrote down why you decided things

**What you have.** 16 decision records explaining each major choice, the measurements behind it, and
what you rejected.

**Why it matters.** In six months you will know *why*, not just *what*. This is a professional habit.
Keep it in every project you do.

---

## PART 2 — Your weaknesses

### Weakness 1: Maturity — this is the big one

| | cognee | Yours |
|---|---|---|
| Stars on GitHub | 30,800 | private |
| Copies made by others | 3,100 | none |
| Code changes over time | 10,270 | 19 |
| Age | years | **6 days** |
| People who built it | many | you |
| Paid support option | yes | no |

**Why it matters.** Thousands of strangers have used cognee in ways nobody planned, found bugs, and
had them fixed. Your system has been used by one person for less than a week.

**What to do:** nothing quickly — you cannot buy maturity. But keep testing on real data, because
that is how you find the same bugs yourself. Every serious bug in your project was found by *running*
it, not by reading it.

---

### Weakness 2: You can only read files

| Type of data | cognee | Yours |
|---|---|---|
| Documents and notes | yes | yes |
| Code repositories | **yes, understands functions** | reads code as plain text |
| Chat conversations | **yes** | no |
| Tickets and issues | **yes** | no |
| Databases | **yes** | no |

**What to do:** decide if you care. For a personal notes memory, files may be enough. If you want your
code properly understood, this is a real gap.

---

### Weakness 3: Your search always works the same way

**The problem.** Every question gets the same treatment: meaning search plus word search, combined.

Ask *"list every project that uses Python"* and that is the wrong approach. That question needs a
graph query, not a text search. Your system has the graph. It just never chooses it.

cognee picks a strategy based on the question type.

**What to do:** this is **your biggest available search improvement — bigger than the reranker**. See
Part 4.

---

### Weakness 4: No memory of conversations

Yours remembers what your **files** say. It does not remember what you and Claude **discussed**.

cognee has sessions, and can move short-term conversation into long-term memory.

**What to do:** decide whether you want this. It is a genuine new feature, not a fix.

---

### Weakness 5: Only tested small, on one machine

Yours has been tested with **241 documents on one laptop**. cognee documents multi-project loading and
very large contexts, plus cloud and distributed setups.

Nothing suggests yours would break at ten times the size. But nobody has tried.

**What to do:** if you plan to grow, test at 10× before you need it, not after.

---

### Weakness 6: More parts to run

Yours needs three things: Postgres, Neo4j, and an embedding service. That is **2.5 GB of memory when
idle**, and Neo4j alone is 1.25 GB.

cognee can run on **one Postgres database** for everything.

Your design has a good reason — Postgres holds the truth and Neo4j is a rebuildable copy, so losing
the graph costs nothing. But it is more to install and keep alive.

---

### Weakness 7: Known problems still open

- 92 facts are wrong
- Your AWS key is an administrator key
- Chunks are far bigger than your search model can read
- No artifact records the sentence it came from

**These matter more than any feature cognee has.** Fix these first.

---

## PART 3 — Where cognee is weaker

To be fair, it is not one-sided.

- **Their single-database mode is labelled a demo feature** in their own documents. Production use
  needs a licence. So the "simpler stack" advantage is not fully available for free.
- **They default to OpenAI.** Out of the box, your data would go to OpenAI unless you change it. Yours
  defaults to a provider you chose deliberately, with a local option.
- **General-purpose means less sharp.** They serve thousands of different uses, so they cannot make
  the strong choices you made — like treating provenance as non-negotiable.
- **You would have to learn their way of working** — their pipelines, tasks, and data units. That is
  real time spent before you get value.
- **No documented time model and no cost tracking**, which are two of your strengths.

---

## PART 4 — What to do next

Work through these in order. Items 1 to 3 come from `next-steps.md`; 4 to 6 are ideas worth taking
from cognee.

### Step 1 — Repair your 92 wrong facts *(do first)*

60 facts say "no longer true" when they are. 32 point the wrong way. Everything needed is ready. About
an hour of work, seconds to run.

**Done when:** Python and PostgreSQL show as current for JobLab again, and your search score has not
dropped.

### Step 2 — Shrink your AWS key

It is currently an administrator key. Make a new one that can only call the one AI model you use. A
few minutes in the AWS console.

**Done when:** extraction still runs, and checking inside the container shows the new limited user.

### Step 3 — Fix chunk sizes

Your search model reads about 190 words. Your chunks reach 10,191 characters. Most of a long chunk is
invisible to it.

**Do this before adding a reranker**, or you will not know which change helped.

**Done when:** no chunk is bigger than your model can read, and you have re-run the search test.

### Step 4 — Add question routing *(borrowed from cognee — biggest win)*

Send different questions to different tools:

- "Which projects use Python?" → graph query
- "What did I decide about the lakehouse?" → search decisions
- "Tell me about X" → the current search

**Why this first among the new features:** you already have the graph and it is populated. You are
just not using it for the questions it is best at. This is cheaper than a reranker and likely to help
more.

**Done when:** list-type questions return complete answers from the graph, and your search test score
goes up.

### Step 5 — Add a reranker

After Step 3. Search finds 50 likely chunks, then a second model reads your question and each chunk
together and re-sorts them. Run it on your own machine.

**Careful:** this fixes *"long general documents beating short specific ones"*. It will **not** fix
the five test questions that fail because the right chunk is never found at all. Those need better
searching, not better sorting.

### Step 6 — Consider session memory *(borrowed from cognee)*

Remember what you and Claude discussed, not only what your files say. This is a new feature, so do it
only once Steps 1 to 5 are done.

### Step 7 — Read cognee's code

It is Apache 2.0 licensed, so you can legally read it, learn from it, and reuse it.

Read specifically how they do **code-symbol extraction** and **question routing**. Reading how a
mature project solved a problem you are about to solve is one of the fastest ways to get better.

---

## PART 5 — Side by side

| | cognee | Yours |
|---|---|---|
| Maturity | **30,800 stars, years old** | 6 days, one user |
| Proof of where facts came from | tracked | **full chain with fingerprint** |
| Knows when facts were true | not documented | **yes** |
| Cost and token tracking | not documented | **yes, per call** |
| Quality measured | standard test, made-up data | **your own documents** |
| Write protection | not documented | **two switches, confirm, logged** |
| Kinds of data read | **code, chat, tickets, databases** | files only |
| Picks search strategy | **yes** | no |
| Remembers conversations | **yes** | no |
| Running it | **cloud, distributed, self-host** | one machine |
| Parts to install | **can be one database** | three |
| Help when stuck | **community** | you |

---

## The bottom line

**Your weakness is maturity and breadth.** cognee has been tested by thousands of people. Yours has
not. You cannot fix that quickly, and you should be honest with yourself about it.

**Your strength is that you know exactly what your system does and does not do — because you measured
it.** You can state that search works 67% of the time, that 23% of your AI spending was wasted, that
92 facts are wrong, and that every fact traces back to a file fingerprint.

Most people running a memory system cannot tell you a single one of those numbers about their own.

That habit — measuring instead of assuming — will do more for your career than any feature in either
system.
