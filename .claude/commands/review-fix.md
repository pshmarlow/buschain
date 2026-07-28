<!-- This is a Claude Code command file. -->

Fix issues found by `/review` or `/review-full`. Follow these steps:

1. Determine the source of findings. If I pasted review output or if findings from a `/review` or `/review-full` run exist earlier in this conversation, use those. If I specified particular files or issues to fix, use those instead. If there is nothing to work from, tell me to run `/review` or `/review-full` first and stop.

2. Read CLAUDE.md for project context, conventions, and any documents it references.

3. Check if docs/review-criteria.md exists. If it does, read it — it provides context on what the project considers critical and fragile.

4. Parse the findings into a work list. For each finding, extract:
   - The file and line(s)
   - The severity (bug, risk, question)
   - What the code does vs. what it should likely do

5. Sort the work list: bugs first, then risks, then questions.

6. Work through findings one at a time. For each finding:

   a. Read the relevant file and surrounding context (not just the flagged lines — understand what the code is doing).

   b. Determine if the finding is valid. Sometimes review findings are false positives, especially for **question**-severity items. If the code is actually correct, say so and explain why, then move to the next finding.

   c. If the finding is valid, propose a fix. Show:
      - The file and lines being changed
      - The current code
      - The proposed replacement
      - A brief explanation of what the fix does and why

   d. Wait for my confirmation before applying the fix. Accept these responses:
      - **y / yes** — apply the fix as proposed
      - **n / no / skip** — skip this finding, move to the next
      - **edit** or a description of changes — adjust the fix and re-propose
      - **stop** — stop fixing, leave remaining findings for later

   e. After applying a fix, verify it doesn't break the immediate surrounding code. If the fix touches a function signature, check callers. If it changes a return type, check consumers. Flag any ripple effects before moving on.

7. After all findings are processed (or stopped early), provide a summary:
   - How many findings were fixed, skipped, and determined to be false positives
   - List of files modified
   - Any ripple effects or follow-up items that need manual attention

8. If the project has a test command documented in CLAUDE.md or package.json scripts, suggest running it. Do not run tests automatically — the user may prefer to review changes first.

9. Do NOT:
   - Apply any fix without explicit confirmation
   - Refactor or improve code beyond what the finding requires
   - Change code style, formatting, or naming while fixing
   - Add or modify tests (mention if a test should be updated, but don't change it)
   - Fix multiple findings in a single step — always one at a time