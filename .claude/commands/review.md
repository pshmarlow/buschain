<!-- This is a Claude Code command file. -->

Review the changes in this project for logic errors. Follow these steps:

1. Determine the review scope. If I provided a git ref or range (e.g. HEAD~3, main..feature), use that. Otherwise default to the most recent commit: `git diff HEAD~1`.

2. Read CLAUDE.md for project context, conventions, and any documents it references.

3. Check if docs/review-criteria.md exists. If it does, read it and apply those project-specific review criteria to the diff. If it doesn't, proceed with general review criteria only.

4. Review the diff for:
   - Logic errors: wrong comparisons, off-by-one, sign errors, incorrect operator precedence
   - Edge cases: division by zero, empty inputs, null/undefined values, boundary conditions
   - Data flow: type mismatches between functions, lost or silently dropped data, implicit conversions
   - Consistency: behavior that contradicts project documentation or established patterns elsewhere in the codebase
   - Integration points: mismatched assumptions between components, API contracts not honored, error responses not handled

5. Do NOT flag or comment on:
   - Code style, formatting, or naming conventions
   - Refactoring opportunities
   - Performance optimizations (unless they cause correctness issues)
   - Missing tests (unless a specific test gap is documented in review criteria)

6. For each finding, report:
   - The file and relevant line(s)
   - What the code does vs. what it should likely do
   - Severity: **bug** (will produce wrong results), **risk** (will produce wrong results under specific conditions), or **question** (ambiguous intent that should be verified)

7. If you find nothing actionable, say so briefly. Do not pad the review with low-confidence observations.

8. After reporting findings, check whether any finding represents a pattern not already covered in docs/review-criteria.md. If so, propose specific additions — a new domain-specific check, a new known fragile area, or a new critical path. Show exactly what you would add and where. Ask for confirmation before editing the file. If there are no new patterns worth capturing, skip this step silently.

9. After step 8, count the total items in docs/review-criteria.md. If the count exceeds 30, mention it briefly: "Criteria doc is at [N] items — consider running /review-criteria-prune to keep it focused." Do not prune or modify anything yourself in this step. If the count is 30 or under, skip this step silently.
