---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

lưu datasource Google News thì báo lỗi, sửa giúp mình. response:
```
POST /api/v1/connectors 422
{"detail":[{"type":"string_too_short","loc":["body","config","news_query"],"msg":"String should have at least 1 character","input":"","ctx":{"min_length":1}}],"request_id":"0193f2a1-7c3e-7b10-9a55-2e4c1d8b6f01"}
```
mình chỉ điền site reuters.com, để trống ô query
