---
on: TextSubmitted
agent: Writer
output:
  rows: str
---
**Prompt chaining, step 1 of 3.** The chain decomposes one hard transformation into a
fixed sequence of easy ones, each handing its output to the next.

From the submitted text, pull out every metric mentioned and its value:

{{ event.text }}

Return one `name: value` pair per line as the `rows` output. Don't format or sort yet —
later steps do that.
