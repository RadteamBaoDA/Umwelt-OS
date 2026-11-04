---
type: llm
focus: trace
weight: 1
---

The reply identifies modules/connectors/public.py news_query (Field min_length=1) and the clean_news_query before-validator returning an empty string instead of None as the cause, or the web form sending an empty string for an optional field, with the shared validator named as the fix site.
