# AI Memory V0.1 — What Happened, and What It Teaches

**Written:** 2026-09-18 · **For:** the owner of this project, as a learning document.

This is not a status report. That is `final-build-report.md`. This one explains **how the system got
built, what went wrong, what it cost, and what you should do differently next time.**

It is written for someone learning to be an AI engineer. Technical words are explained the first time
they appear, because you will need that vocabulary — avoiding it would not help you.

Every number here is real and measured. Where a number could not be measured, it says so instead of
guessing.

One more thing. A lot of the waste below was **my** fault, not yours — poor instructions to the
helper agents, and wrong diagnoses I handed them. Those are marked. A review that blames only you
teaches you nothing.

---

## 1. What you built

| | |
|---|---|
| Code | 134 files, 30,658 lines |
| Tests | 69 files, 17,766 lines — **810 tests, all passing** |
| Documents ingested | 241 files from your vault |
| Knowledge extracted | 614 entities, 1,244 facts, 729 artifacts |
| Time | 6 days (not continuous) |
| Bedrock cost | $3.20 |

**Some words you will keep meeting:**

- **Entity** — a thing the system knows about: a project, a person, a technology.
- **Fact** — a statement linking two entities. "JobLab uses Python."
- **Artifact** — a piece of knowledge that is not a simple link: a decision, a task, a requirement.
- **Chunk** — a document cut into a readable piece, usually a few paragraphs. The system searches
  chunks, not whole files.
- **Embedding** — a list of numbers representing the meaning of a chunk. Two chunks about similar
  topics have similar numbers. This is how meaning-based search works.
- **Provenance** — the record of where a fact came from: which file, which version, which AI model.

Your system has all of these working.

---

## 2. What it cost you

### 2.1 The helper agents

I used 24 helper agents. Each one is a separate Claude working on one job. Fifteen finished. They
used about **2.84 million tokens** and made about **1,471 tool calls** between them.

A **token** is roughly three quarters of a word. Tool calls are things like reading a file or running
a test.

### 2.2 The interruptions

**9 of the 24 agents (37.5%) were killed part-way through** when the session hit its limit. One
earlier session was lost to a power cut.

But *how* an agent dies matters more than *that* it dies. Three things happened:

| What happened | Example | How bad |
|---|---|---|
| Died before writing anything | Two agents | **Harmless.** Just start it again. |
| Wrote code but never tested it | The MCP server agent left 2,022 lines untested | **Annoying.** I had to check its work myself. |
| **Wrote documentation for a fix it never made** | The chunker agent | **Bad.** The file now *lied*. |

That last one is the important lesson. An agent was interrupted after it had updated a comment
describing a fix, but before it wrote the fix. Anyone reading that file would believe the problem was
solved. It was not.

**What this teaches you:** if work can be interrupted at any moment, the order matters. Always
**build it, test it, then write about it**. Never the other way round. A half-finished job that
describes itself as finished is worse than no job at all.

### 2.3 Where the days went

Roughly: 45% building new things, **30% fixing mistakes**, 15% checking the work, 10% writing reports.

That 30% is the number to attack. Section 4 explains where it went.

---

## 3. Changing from a local AI to Bedrock

Part-way through, you switched from running a small AI on your own laptop (Ollama with Qwen3-4B) to
using Claude Haiku on AWS Bedrock.

### 3.1 It was the right call

Real measurements, 20 documents each:

| | Local AI | Bedrock Haiku |
|---|---|---|
| Time per document | **174 seconds** | **7.7 seconds** |
| Found the right relationships | 40% | **61%** |
| Produced valid output | 95% | **100%** |

**22 times faster.** Extracting your vault locally would have taken about **7 hours**. It took 35
minutes. On your laptop (4 cores, no graphics card), the local AI was not just slow — it was too slow
to work with at all.

### 3.2 But changing mid-build cost you

The decision was right. Making it **half-way through** was the expensive part, because everything
already built assumed the old choice:

1. **No credentials reached the containers.** Your setup said "use Bedrock" but no AWS login details
   were passed anywhere, and the AWS library was not even installed. A whole agent had to fix this.
2. **A new security problem appeared.** The AWS key now mounted into the containers is an
   administrator key — see section 9.
3. **Your private documents left your computer.** 146 documents went to AWS, including job
   applications and motivation letters naming real employers.
4. **New machinery had to be built** to stop two different AIs mixing their results in one database.

**What this teaches you:** decide the big, expensive things **first**. Which AI you use is not a small
detail. It touches your containers, your passwords, your privacy, and your costs. The way to catch
this early is a **thin end-to-end test in week one** — one document, one extraction, one search, using
the real setup. That would have found all four problems on day two.

### 3.3 Where the $3.20 went

About **190 documents were paid for. Only 146 were kept.**

| What | Share | Roughly |
|---|---|---|
| Real work you kept | 77% | $2.45 |
| Testing which AI to use | 12% | $0.40 |
| **Lost to bugs** | **11%** | **$0.35** |

**The session stops cost you nothing here.** That is worth being clear about. The session limits
killed *helper agents*, which spend Claude tokens. The Bedrock money was only spent by extraction
runs, which I started from the command line. Those were never killed by a session limit — they
crashed on bugs, or they finished.

Three bugs threw money away:

- **14 documents** — the AI read them and wrote out the facts. Then saving to the database failed.
  Paid for, thrown away.
- **7 documents** — they contained dates like `2022-03`. The code could not read that and crashed.
  These were paid for three times before the fix worked.
- **1 document** — crashed the first run completely, because a settings file pointed at a project that
  did not exist.

One detail makes this worse. **Writing costs about five times more than reading.** In all three cases
the AI had already finished writing before the failure. So the waste landed on the expensive half.

**What this teaches you:** all three failures happened **after** the AI did its work. Checking a date
format or a settings file costs nothing. Do the cheap checks **before** you pay for the expensive
work. That one change would have saved nearly all of the $0.35.

The $0.40 spent on testing was **not** waste. It is why extraction took 35 minutes instead of 7 hours.

---

## 4. The mistakes that cost the most

Nine problems ate about 30% of the work. They come from five root causes — and **four of the five are
design problems, not bad luck.**

### 4.1 Code lived in two places at once

Your system runs in **containers** — small isolated boxes, each running one service. Code gets into a
container two ways: **baked in** when the container image is built, or **mounted live** from your
folder.

You had both. So the same code existed in two versions, and they drifted apart silently.

This caused four separate failures, including one where the database said "I am at version 3" and the
container said "I have never heard of version 3."

Worse: two services were built from the same file but produced **two different images**. Rebuilding
one left the other stale.

**What this teaches you:** pick **one** way for code to reach the running system. If you must have
two, make each service announce its version on startup and fail loudly when it disagrees with the
database. Your system already had the information to notice this. Nothing ever checked.

### 4.2 One database doing three jobs

The same database was your **real data**, your **test data**, and your **testing target**.

The results:

- One test wipes the database on purpose to check that setup scripts work. It **destroyed your entire
  extracted vault. Twice.** Nobody noticed the first time.
- A test passed on an empty database and failed on a real one. That is backwards — tests should get
  *more* reliable with real data.
- A dashboard test triggered a **real ingestion** of your project folder, unattended.

**What this teaches you:** keep them separate from day one. A throwaway database for tests costs
almost nothing to set up. Losing a corpus you paid 35 minutes of AI time to build costs a lot.

### 4.3 Nobody measured search quality until the end

**This is the single most expensive mistake in the whole project.**

Search was designed, built, extended, and connected to Claude before anyone checked whether it
returned the right answer.

When it was finally measured: **33%**. The right document appeared in the top five results only one
time in three.

The cause was structural. Search combined two scores:

- a **ranking score** that varied by only 0.006 between the best and worst result
- **bonus points** worth up to 0.15

The bonuses were **30 to 50 times larger than the actual relevance signal**. So results were not
really sorted by relevance at all. They were sorted by bonus points, with relevance as background
noise. This was true from the first line of code and survived four phases of building.

Something similar happened with the knowledge graph. Search was supposed to follow connections
between entities. But the link between "this chunk mentions this entity" was never filled in — every
row was empty. The feature was built, tested against a fake stand-in, connected up, and was
**incapable of ever working on real data.**

**What this teaches you — the most important lesson here:** write your test questions **before** you
build the search. Twenty-five questions with known answers takes about two hours. It would have
caught both problems in week one, and it makes every later change measurable instead of guesswork.

There is a name for this: a **gold set** (sometimes "evaluation set"). Professional AI teams build it
first. Now you know why.

### 4.4 Rules that were written down but never enforced

Your design document says a project has an owner: `Project → HAS_OWNER → Person`.

Nothing checked this. So **38 of 58 ownership facts were stored backwards** — saying a person has a
project as its owner.

A second rule said certain facts can only have one value at a time. That is right for "status" — a
project cannot be both active and finished. It was wrongly applied to "uses technology". So each time
the AI found a new technology, it marked the previous one as **no longer true**. Your database
recorded that JobLab had **stopped using Python and PostgreSQL**. It has not.

**What this teaches you:** a rule that nothing enforces is just a comment. If a rule matters, the code
must refuse to break it. And each rule needs a clear test for when it applies — otherwise the list
grows by guesswork. We eventually wrote that test down: *a rule is "one value only" if having two at
once would be a contradiction, not merely unusual.*

### 4.5 Settings that pointed at things that did not exist

A settings file mapped nicknames to project names. **Seven of eighteen project names did not exist.**

The database enforces that a project must exist before anything can point at it. This is called a
**foreign key** — a rule saying "this reference must point at something real." The first bad nickname
killed the whole extraction run, after 4 of 129 documents, with the AI cost already paid.

**What this teaches you:** if one file refers to another, write a test that checks they agree. Five
lines. It would have prevented the outage.

### 4.6 A safety net with a hole in it

The code that processes documents had protection around the AI call, with a comment saying *"never
lose the queue to one bad document."*

The saving step right below it had **no protection**. The failure happened there, escaped, and
abandoned 125 documents.

**What this teaches you:** when you write a comment promising something, check the code actually does
it. The comment was right. The code did half of it.

---

## 5. Working with helper agents

### 5.1 What worked

**Give each agent its own files, and say what it must not touch.** Three agents worked at the same
time with no conflicts because each was told exactly which files were off-limits, and why.

**Ask for proof, not claims.** Instructions like *"paste the output showing the container is healthy —
a passing test is not proof"* caught several agents that said "done" when it was not. One security
agent made 184 tool calls proving its findings rather than just reading code.

**Tell agents to check my thinking, not trust it.** This kept paying off:

- I gave one agent a fix. It read the file properly and found a **second identical bug** one step
  later that my fix would have missed.
- I told one agent there were eight changes to record. It checked and found **nine**.
- One agent found a bug in its own test — it had been comparing database keywords against rule names.

**Use a frozen contract between agents.** Two agents built either side of a connection without ever
reading each other's code, because the shape of that connection was written down in a file both
agreed on, with a test making sure neither drifted.

### 5.2 What did not work

**I protected files, but not data.** Every serious clash was two agents writing to the **same
database**, never the same file. One agent's test triggered a real ingestion while another was
measuring. **Ownership needs to cover data too: which tables may this agent write to?**

**I ran dependent jobs at the same time.** I started two agents together when the second needed the
first to finish. Both were killed. When work resumed, the first agent's change had been written but
never applied. Running things in parallel is not free when one needs the other.

**My early instructions were too thin.** Later ones included the current numbers, the exact trap to
avoid, the forbidden files *with reasons*, and what proof to return. Same agents, much better work.

**The instruction you give an agent is the highest-value thing you write.** Not the code — the brief.

**I never told agents to save their place.** With more than a third being killed, every agent should
have been told: *"if you are running out of time, stop somewhere clean and say exactly where you
got to."* I gave that instruction once. It worked. It should have been in every brief.

---

## 6. Making search better

You asked about **reranking**. Here it is properly.

### 6.1 Why search stalls where it is

Right now search works like this. Every chunk is turned into a list of numbers once, in advance. Your
question is turned into numbers too. Then it finds the chunks whose numbers are closest.

This is a **bi-encoder** — "bi" because the question and the document are processed **separately**,
and only their numbers ever meet. It is fast, because the chunk numbers are worked out ahead of time.

Two problems follow from that:

- **Long vague documents win.** A long document that mentions all your words somewhere scores well,
  even if it is not about your topic. In your test set, a general "how to write a CV" guide still
  beats the specific job posting you were actually asking about.
- **Long chunks get cut off.** The model only reads the first **256 tokens** (about 190 words) of a
  chunk. Your chunker produced chunks up to 10,191 characters. Most of those chunks are invisible.

### 6.2 What a reranker does

A **cross-encoder** reads your question and one chunk **together**, at the same time, and scores how
well that chunk answers that question. Because it sees both at once, it can tell the difference
between a document that is *about* your topic and one that merely *mentions* it.

The catch: it cannot be worked out in advance. Every question-chunk pair needs its own calculation. So
you cannot run it over 4,757 chunks.

The standard solution is two stages:

```
your question  →  fast search finds 50 likely chunks
               →  reranker carefully scores those 50
               →  return the best 5
```

### 6.3 What it would and would not fix

**It would fix** the "long vague document wins" problem. That is exactly what rerankers are good at,
and it is your main remaining issue.

**It would not fix** the five test questions that still fail. For all five, the right chunk is **not in
the 50** the first stage found. A reranker only reorders what was already found.

That distinction has names worth learning:

- **Precision** — of what you found, how much was right. Rerankers fix this.
- **Recall** — of what was right, how much did you find. Rerankers do not fix this.

**Work out which problem you have before buying a solution.**

### 6.4 What to do, cheapest first

1. **Fix chunk sizes first.** Your model reads 256 tokens; your chunks reach 10,191 characters. Cut
   chunks to fit. This is small, free, and must come first — otherwise you cannot tell which change
   helped.
2. **Then add a reranker** that runs on your own machine (`bge-reranker-base` or
   `ms-marco-MiniLM-L-6-v2`). Roughly 100–500 milliseconds per search, guessed not measured. Nothing
   leaves your computer.
3. **Measure each change separately** against your test questions.

We already tried simply looking at more results without a reranker. It made things **worse** (60%
versus 66.7%). More results without better sorting is just more noise.

---

## 7. How to work with Claude well

### 7.1 Managing what Claude can see

Claude can only hold so much at once. This is its **context**. Everything counts: your messages, files
it reads, output from commands it runs.

**Helper agents are a way of managing context, not just a way of going faster.** Each agent's file
reads and test output stayed in *its* context, not mine. That is the only reason one conversation
could run 24 agents over six days. Handing off a job you could do yourself is often right **purely to
keep the noise out of your own context.**

**Put the facts in the brief.** Every fact an agent has to go and find costs tokens and might be found
wrong. My later briefs opened with the current numbers and the exact file containing the trap.

**Name the trap.** *"This folder name has a dash in it and cannot be used as a code module — this
exact bug broke another service for days, copy the working pattern"* is one sentence that saves an
agent an hour.

**Ask for proof, not an outcome.** "Make it work" invites a claim. "Show me the output" invites proof.

**Protect your measuring stick.** When I asked an agent to improve search, I told it plainly: **do not
edit the test questions.** If an agent can change its own exam, its score means nothing. It did not
edit them, which is why the improvement from 33% to 67% is believable.

### 7.2 Working habits

- **Build, test, then document.** Never the other way round.
- **Ask agents to save their place** if they might be interrupted.
- **Ask agents to check your thinking.** I was wrong at least three times and agents caught all three.
- **Run only the tests you need.** A full test run takes two minutes and, before we fixed it,
  destroyed your data.
- **Save your work at every working point**, not at the end of the day.

### 7.3 What I should have done differently

Built the dependency order before running agents in parallel. Put a save-your-place instruction in
every brief. Insisted on a thin end-to-end test before building the plumbing. And treated "the
reports are out of date" as a real problem rather than a chore — the reporting agent ran twice and was
out of date both times.

---

## 8. A better plan for next time

Same phases, better order. Changes are marked.

| # | Phase | Why here |
|---|---|---|
| 0 | Check the machine and tools | unchanged — worked fine |
| 1 | **One document, end to end** | **NEW, and the biggest change.** One document in, one extraction, one search — using real containers and real credentials. Finds the expensive surprises on day two. |
| 2 | Write the rules **and make the code enforce them** | **CHANGED.** A rule nothing checks is a comment. |
| 3 | **Write 20–30 test questions — before any search code** | **MOVED from near the end.** The highest-value change on this list. |
| 4 | Infrastructure, with **one way for code to reach the system** | **CHANGED.** |
| 5 | Database, with **separate real / test / throwaway databases** | **CHANGED.** |
| 6 | Reading and extracting documents | unchanged |
| 7 | Search, **measured against the test questions after every change** | **CHANGED.** |
| 8 | Knowledge graph and history | unchanged |
| 9 | API and the Claude connection | unchanged |
| 10 | Security review — **early enough to change the design** | **MOVED earlier.** An over-powerful key is a design problem, not a note for the end. |
| 11 | Dashboards, docs, clean install test, final report | unchanged |

Three rules worth keeping forever:

1. **Nothing is finished until something measures it.** Both big failures passed their own tests and
   were broken on real data.
2. **If one file refers to another, test that they agree.**
3. **Test a clean install every week, not once at the end.**

---

## 9. Weak spots in your system

The strongest part first, because it is worth protecting.

**What is genuinely excellent: provenance.** Every one of your 1,244 facts records the file it came
from, that file's exact fingerprint, the version, your machine, which AI model read it, and which run
produced it. **Not one fact is missing this.** You can ask "where did this come from?" and get the
full chain back.

Most memory systems cannot do this. It is also the hardest thing to add later. You got it right.

| # | Weak spot | State |
|---|---|---|
| 1 | Search quality measured only at the end | Fixed late — 33% to 67%. The ordering lesson stands. |
| 2 | One database doing three jobs | Partly fixed |
| 3 | Code living in two places | **Open** — nothing checks for drift |
| 4 | Rules not enforced when writing | Fixed |
| 5 | "One value only" rule had no test for when it applies | Fixed |
| 6 | Settings not checked against real data | Settings fixed, **but no test guards it** |
| 7 | Search mixed two scores of wildly different sizes | Fixed |
| 8 | Chunk-to-entity link never filled in | Fixed |
| 9 | No artifact records the sentence it came from (0 of 729) | **Open** |
| 10 | Chunks far longer than the model can read | **Open** — see section 6 |
| 11 | 32 facts stored backwards, 60 wrongly marked as ended | **Open** — repair ready, waiting for your decision |
| 12 | The AWS key is an administrator key | **Open** — needs a change in your AWS account |

---

## 10. If you remember only one page

1. **Write your test questions before you build the search.** Everything else follows from getting
   this order wrong.
2. **A rule nothing enforces is just a comment.** 38 backwards facts and 60 wrongly-ended facts came
   from two written rules that nothing checked.
3. **One way for code to reach the running system.** Most of the deployment pain was two copies
   disagreeing.
4. **Never test against your real data.** It cost you the whole corpus twice.
5. **The instruction you give an agent matters more than the code you write.** Current numbers, the
   named trap, forbidden files with reasons, and what proof to bring back.
6. **Build, test, then document** — never the reverse, if you might be interrupted.
7. **Do the cheap checks before the expensive work.** Every wasted dollar was spent *after* the AI had
   already finished writing.
8. **Rerankers fix precision, not recall.** Work out which one you are missing first.
