---
type: llm
focus: trace
weight: 1
---

The proposed fix normalizes empty or whitespace-only input to None (in clean_news_query or an equivalent shared place) rather than removing the min_length constraint.
