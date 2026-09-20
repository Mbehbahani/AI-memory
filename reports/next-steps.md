# AI Memory V0.1 — What To Do Next

**Date:** 2026-09-18. Ordered by value for effort. Each item says what it fixes, what it costs, and
how you will know it worked.

---

## Do these first — cheap, and they remove known-wrong behaviour

### 1. Repair the 92 wrong facts

**What it fixes:** 60 facts are marked as "no longer true" when they still are. 32 ownership facts
point the wrong way. Until this runs, asking "what does project X use?" gives an incomplete answer.

**What it costs:** about an hour to write and check. Seconds to run. It touches 92 rows.

**Why it can run now:** the two things it depended on are done — the database rule is updated, and
the code that writes facts now puts them the right way round. Repairing before that would have been
undone by the next extraction.

**How to do it safely:** copy the facts table first. Only re-open facts that were closed by the rule
we now know was wrong — never one that was genuinely replaced. Use the code to flip direction, not
hand-written database commands. Then rebuild the graph and re-run the search test.

**You will know it worked when:** Python and PostgreSQL show as current for JobLab again, ownership
facts start with the project rather than the person, and the search score has not dropped.

### 2. Shrink your AWS key (the one High security finding)

**What it fixes:** the key mounted into your containers is an **administrator** key. It can do
anything in your AWS account. Read-only mounting stops the container changing the file, but does
nothing about what the key can do over the internet.

**What it costs:** a few minutes in the AWS console. Nothing in this project.

**How:** create a new user whose only permission is to call the one Claude model you use. Point the
project at that instead. Consider mounting only that one profile rather than your whole AWS folder,
which currently also exposes cached login sessions.

**You will know it worked when:** extraction still runs, and checking the identity inside the
container shows the new limited user.

### 3. Add a test that settings match reality

**What it fixes:** the kind of failure that killed an extraction run after 4 of 129 documents.

**What it costs:** about ten lines.

**How:** check that every project name in the settings file actually exists in the database. Fail at
test time, loudly, instead of mid-run.

---

## Then — raising the quality ceiling

### 4. Fix chunk sizes, then add a reranker

**What it fixes:** the main remaining search problem — long general documents beating short specific
ones.

**Order matters here.** Your AI model only reads the first 256 tokens (about 190 words) of a chunk,
but your chunks reach 10,191 characters. Most of a long chunk is invisible to it. Fix that first,
measure, **then** add the reranker — otherwise you cannot tell which change helped.

**How:** search finds 50 likely chunks, then a second model reads your question and each chunk
together and re-sorts them, and you return the best 5. Use `bge-reranker-base` or
`ms-marco-MiniLM-L-6-v2`, running on your own machine. Section 6 of the retrospective explains this
properly.

**You will know it worked when:** the search score rises above 67% and the "general CV guide beats
the specific job posting" failures disappear. Note that 5 of the current failures will **not** move —
for those, the right chunk is never found in the first place, which is a different problem.

### 5. Re-check the write log, then consider enabling writes

**What it fixes:** the safety net that makes letting Claude write to your memory safe. The bug is
fixed but has only been checked once.

**How:** restart the API while writes are happening, then confirm nothing was dropped and every
attempt appears in the log.

**Do not enable writing before this passes.**

### 6. Give artifacts their source sentence

**What it fixes:** none of your 729 artifacts records the sentence that produced it. Facts have
excellent source records; artifacts are weaker.

---

## When convenient

7. **Update the design document** that still describes the old search scoring method.
8. **Fix model tracking** after mixing two AIs. Needs a decision: re-stamp facts when confirmed
   (losing which AI found it first), or add a separate record.
9. **Make each service announce its code version** on startup and fail when it disagrees with the
   database. This one problem caused four separate bugs.
10. **Separate the remaining tests** that still share your real database.
11. **Try a clean install on a different computer.** The test we ran proves the project folder is
    complete, but it reused this machine's downloaded images.
12. **The JobLab project.** 64 files are already read and indexed; extraction is switched off at your
    request. Turn the setting back on and re-run if you want it. About 15 minutes of AI time.

---

## Not recommended

- **Re-extracting everything** to fix the 92 wrong facts. It costs about 35 minutes of AI time, and
  because the AI gives slightly different answers each time, it would invalidate your search test
  scores and every measured number in these reports. The targeted repair is smaller and keeps your
  baseline.
- **Tuning the search weights against the current test set.** Three different weightings all give the
  same score. Only one secondary measure moves. Optimising that on 24 questions is fitting to the
  test, not improving the system. Write more test questions first.
