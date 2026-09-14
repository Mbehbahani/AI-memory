---
title: note with an embedded JWT-looking string
---

# Note with an embedded JWT-looking string

This file has an innocuous ``.md`` filename (no filename pattern matches it) but its *content*
contains a JWT-shaped string, so it must be caught by the content-based `jwt` regex rule, not the
filename rules. The value below is fabricated (not a real token, not decodable to real claims):

eyJhbGciOiJIUzI1NiJ9.eyJmaXh0dXJlIjp0cnVlLCJyZWFsIjpmYWxzZX0.ZmFrZS1zaWduYXR1cmUtbm90LXJlYWw
