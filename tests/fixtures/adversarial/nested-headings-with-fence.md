# H1 top

Intro text under H1.

## H2 second level

Text under H2.

### H3 third level

Text under H3.

#### H4 fourth level

Text under H4.

##### H5 fifth level

Text under H5.

###### H6 sixth level

Text under H6, the deepest ATX heading level markdown supports.

```python
# This looks like a heading but it is a Python comment inside a fenced code block.
## So does this.
### And this - the chunker must not treat any of these as real headings, and must never split
#### the fence itself in the middle even if it runs long.
def f():
    return "# not a heading either, just a string"
```

## Back to H2

Content after the fence, still under a real (non-fenced) H2 heading. A chunk boundary here should
carry ``heading_path == ["H1 top", "Back to H2"]``, not anything picked up from inside the fence
above.
