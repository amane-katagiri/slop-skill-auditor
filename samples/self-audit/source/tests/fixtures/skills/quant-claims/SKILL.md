---
name: quant-claims
description: Structured code review checklist. Use when the user asks for a code review of a diff or pull request.
---

# Code review checklist

Go through the diff in this order and report findings grouped by severity (blocker, major, minor):

1. Correctness: off-by-one errors, null/None handling, error paths, concurrency.
2. Security: injection, secrets in code, unsafe deserialisation, missing authorisation checks.
3. Tests: new behaviour covered, edge cases, no tests that assert implementation details only.
4. API and naming: breaking changes called out, names describe intent.
5. Performance: accidental quadratic loops, N+1 queries, unbounded memory.

For each finding give file:line, the problem, and a concrete fix.
