<!-- This is a Claude Code command file. -->

Review and prune docs/review-criteria.md to keep it focused and effective. Follow these steps:

1. Read docs/review-criteria.md in full. Count the total number of items across all sections.

2. Read CLAUDE.md and scan the current project structure to understand what exists now.

3. For each item in the criteria doc, evaluate:
   - **Stale**: Does it reference code, files, patterns, or dependencies that no longer exist in the project? If the codebase has moved on, the criterion is dead weight.
   - **Redundant**: Is it substantially the same as another item? Two entries that check for the same class of bug in slightly different wording should be consolidated into one.
   - **Overly specific**: Does it reference a specific file, line, or variable name instead of describing a general pattern? Specific entries break when code moves. Propose a generalized version.
   - **Contradictory**: Does it conflict with another item, possibly added after a design change? The newer intent should win, flag for confirmation.
   - **Still relevant**: Does it match real code patterns that still exist? Keep it.

4. Present findings in three groups:
   - **Remove** — stale or redundant items, with a brief reason for each
   - **Consolidate** — groups of similar items that should merge into one, with the proposed merged wording
   - **Generalize** — overly specific items, with the proposed rewrite

5. Do NOT remove or edit anything without confirmation. Show the full proposed changes and wait for approval.

6. After applying changes, report the new item count.

7. If the criteria doc has fewer than 15 items and nothing looks stale, say so and skip the rest. Don't prune for the sake of pruning.