---
name: missing-impl
description: Prepare a release - bump notes and generate the CHANGELOG. Use when the user says they are cutting a release.
---

# Release helper

1. Run `python3 scripts/changelog.py --since <last-tag>` to generate the CHANGELOG section.
2. Edit the generated text so it follows [the style guide](references/style.md).
3. Show the user the result and ask before committing.
