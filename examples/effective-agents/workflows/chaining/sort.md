---
after: format
agent: Writer
output:
  result: str
---
**Prompt chaining, step 3 of 3.** Sort the table's rows by `Value`, descending, keeping
the header in place:

{{ format.table }}

Return the sorted table as the `result` output — the chain's final answer.
