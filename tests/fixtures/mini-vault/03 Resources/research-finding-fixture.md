---
title: Research finding - fixture retrieval latency
project: fixture-project
tags: [finding, research]
date: 2026-09-08
---

# Finding: hybrid retrieval beats vector-only on the fixture gold set

On the tiny fixture gold set, combining vector search with keyword search via reciprocal rank fusion
(see [[Fixture Project]]) improved recall@5 over vector-only search. Follow-up: extend to the real
`my-vault` gold set once [[requirement-fixture-storage]] lands.

```python
# a code fence inside a research note - the extractor's link scanner should not choke on this,
# and the chunker must never split inside it even if the fence text below is long.
def looks_like_a_heading_but_is_not():
    text = "# Not a real heading, this is a Python comment/string inside a fence"
    return text
```

Confidence: medium (fixture data is small).
