---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

e2e sources-wizard fail đúng 1 test YouTube:
```
  1) [chromium] › tests/e2e/sources-wizard.spec.ts › wizard › YouTube channel feed activates
    Error: expect(received).toBe(expected)
    Expected: "active"
    Received: "error"
    activation issue: {"code":"provider_rejected","detail":"upstream returned HTTP 404"}
```
fix giúp
