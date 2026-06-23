---
after: extract
agent: Writer
output:
  table: str
---
**Prompt chaining, step 2 of 3.** Turn the extracted pairs into a clean two-column
Markdown table with the headers `Metric` and `Value`:

{{ extract.rows }}

Return just the table as the `table` output.
