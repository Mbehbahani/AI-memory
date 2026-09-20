# AI Memory V0.1 — Final Build Report

**Date:** 2026-09-18 · **Verdict: it works, with known problems listed below.**

Companion documents: `retrospective-and-playbook.md` (what happened and what it teaches),
`known-limitations.md`, `next-steps.md`, `deploy-verification.md`, and
`evaluation-20260918T043955Z.md` (the search quality test).

---

## 1. The honest verdict

**What you can use today:** ingest your vault, search it and get answers that show exactly where they
came from, browse the knowledge as a graph, and connect Claude to it. The whole system starts from
scratch on a clean machine with no AWS setup needed.

**What you should not rely on yet:** search finds the right document in the top five **two times out
of three**. Your database holds **92 facts known to be wrong**. And one security problem needs a
change in your AWS account that only you can make.

Nothing in this report is guessed unless it says so. Search quality is given as a measured number
because for most of the build it was genuinely unknown.

---

## 2. What got built

| Phase | What happened |
|---|---|
| Setup | Machine checked, rules written, project structure created |
| Infrastructure | Databases and services running, locked to your machine only |
| AI choice | Compared two AIs on real documents; picked Claude Haiku on Bedrock |
| Database | Tables, indexes, and upgrade scripts |
| Reading documents | Text extraction, splitting into chunks, secret detection |
| Ingesting | Your vault read and indexed |
| Extracting knowledge | Facts pulled out of 146 documents by the AI |
| Search | Meaning-based and word-based search, combined and ranked |
| API and Claude connection | A web service, plus 12 tools Claude can call |
| Dashboards | A graph viewer and an operator page |
| Testing | Test questions written and search quality measured |
| Security | Threat review, 7 findings |
| Clean install | Verified from scratch — passed |
| **Not done** | The JobLab pilot project — you chose to skip it |

## 3. The numbers

| | |
|---|---|
| Tests | **810 passing**, 3 skipped |
| Code | 134 files, 30,658 lines |
| Documents | 241 files, 4,757 chunks |
| Knowledge | 614 entities, **1,244 facts**, 729 artifacts |
| Facts missing their source record | **0** |
| Documents extracted | 146 of 147 (the one skip is an empty file — correct) |
| Bedrock cost | **$3.20** |

### Search quality (tested on 25 questions with known answers)

| What we measured | Before the fix | After |
|---|---|---|
| Right document in the top 5 | 33% | **67%** |
| Right entity mentioned | 72% | 78% |
| **Gap between answerable and unanswerable questions** | **−0.002** | **+0.199** |
| Answers that show their source | 100% | 100% |

That third row matters most. Before the fix, the system was **slightly more confident about questions
it could not answer** than about ones it could. Now it can tell the difference.

### AI comparison (20 documents each)

Claude Haiku on Bedrock: **7.7 seconds per document**, 100% valid output, found 61% of relationships.
The local AI: 174 seconds per document, 40% of relationships. **22 times slower.**

---

## 4. Decisions that shaped the system

- **A ready-made knowledge tool was rejected** after measuring it. It needed 6–10 AI calls per
  document and took 17–29 minutes each locally. We built a simpler one: 2–3 calls per document.
- **Claude Haiku on Bedrock became the default**, overriding the original plan of never sending data
  to the cloud. This was your decision, made with the measurements in front of you.
- **The graph database cannot enforce read-only users** in the free edition. This was written down
  rather than pretended otherwise.
- **Rules about fact direction are now enforced when writing**, not just documented.
- **Claude cannot write to your memory** unless you turn on two separate switches, confirm each write,
  and stay under 10 writes a minute. Every attempt is logged, including refusals.

---

## 5. Problems found by running it on real data

Fifteen real bugs were found by using the system, not by reading the code. The ones that mattered
most:

1. **Search ranking was structurally broken.** Bonus points were 30–50 times bigger than the relevance
   signal, so results were sorted by bonus, not relevance. Fixing it doubled search quality.
2. **The knowledge graph search could never work.** The link between chunks and entities was never
   filled in. Built, tested against a fake, connected up, and incapable of working.
3. **The test suite destroyed your data.** A test that wipes the database on purpose ran against your
   real one. Twice.
4. **38 of 58 ownership facts were stored backwards**, and 24 true facts were marked as no longer
   true — two written rules that nothing enforced.
5. **The system could not start from scratch** — required files were missing from the containers.
6. **Bedrock could not log in from any container** — no credentials passed, and the library was not
   installed.
7. **The write log silently stopped working** after one brief hiccup. 40 of 47 records, including
   every refused write, never reached the database.
8. **The operator page accepted commands from other websites**, which could have triggered a paid AI
   run just by visiting a page.
9. **Dates like "2022-03" destroyed whole documents** — one unreadable date threw away every fact in
   that file.
10. **A settings file pointed at seven projects that did not exist**, killing an extraction run after 4
    of 129 documents.

---

## 6. Open problems

| How serious | What | Who fixes it |
|---|---|---|
| **High** | Your AWS key is an administrator key | **You** — in the AWS console |
| Medium | 92 facts are wrong (32 backwards, 60 wrongly marked ended) | Repair ready, waiting for your decision |
| Medium | The write log fix works but was only checked once | Re-check before enabling writes |
| Low | No artifact records the sentence it came from (0 of 729) | Future work |
| Low | Model tracking under-reports after mixing two AIs | Future work |
| Low | One design document still describes the old scoring method | Future work |
| Low | Nothing tests that settings match reality | Future work |
| — | 5 test questions still fail because the right chunk is never found | Needs a reranker or a better model |

---

## 7. What to trust, and what to check

**Trust the source records.** Every fact traces back to a file, a version, a fingerprint, your
machine, and the AI that read it. Ask "where did this come from?" and you get the full chain. Nothing
is missing. This is the strongest part of the system.

**Check anything about what a project currently uses.** Until the repair runs, some true facts are
marked as ended and will not show up. Some ownership facts point the wrong way.

**Do not turn on writing yet.** The write log is the safety net that makes writing safe, and it has
only been tested once since it was fixed.
