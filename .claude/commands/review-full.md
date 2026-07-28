<!-- This is a Claude Code command file. -->

Review the entire codebase of this project for logic errors. Follow these steps:

1. Determine the review scope. If I specified particular files, directories, or glob patterns, limit the review to those. Otherwise review all source files in the project.

2. Read CLAUDE.md for project context, conventions, and any documents it references.

3. Check if docs/review-criteria.md exists. If it does, read it and apply those project-specific review criteria to the codebase. If it doesn't, proceed with general review criteria only.

4. Build a file list. Run `find` or `git ls-files` to get all source files, excluding:
   - Version control directories (.git)
   - Dependencies (node_modules, vendor, venv, .venv, __pycache__, target, dist, build)
   - Generated files (*.min.js, *.bundle.js, lock files, compiled output)
   - Binary files, images, fonts
   - Test fixtures and snapshot files
   - Configuration files that are purely declarative (JSON/YAML config with no logic)

   If the file list exceeds 80 files, prioritize by reading the critical paths listed in docs/review-criteria.md first, then files with the most logic (sort by size as a rough proxy, prefer .py/.ts/.js/.rs/.go/.java/.rb files). Tell me which files you reviewed and which you skipped.

5. Read files in batches. For each file, review for:
   - Logic errors: wrong comparisons, off-by-one, sign errors, incorrect operator precedence, unreachable code, impossible conditions
   - Edge cases: division by zero, empty inputs, null/undefined values, boundary conditions, integer overflow, empty collections
   - Data flow: type mismatches between functions, lost or silently dropped data, implicit conversions, unvalidated external input flowing into sensitive operations
   - Consistency: behavior that contradicts project documentation or established patterns elsewhere in the codebase, functions that promise one thing in their name/docstring but do another
   - Integration points: mismatched assumptions between components, API contracts not honored, error responses not handled, race conditions in concurrent code
   - Resource management: unclosed handles, missing cleanup in error paths, unbounded growth of in-memory structures
   - Security-relevant logic: authentication/authorization checks that can be bypassed, injection vectors, secrets in source

6. Do NOT flag or comment on:
   - Code style, formatting, or naming conventions
   - Refactoring opportunities or "code smells" that aren't bugs
   - Performance optimizations (unless they cause correctness issues like timeouts or OOM)
   - Missing tests (unless a specific test gap is documented in review criteria)
   - TODOs or FIXMEs (unless they mark a known-broken code path that is reachable)

7. For each finding, report:
   - The file and relevant line(s)
   - What the code does vs. what it should likely do
   - Severity: **bug** (will produce wrong results), **risk** (will produce wrong results under specific conditions), or **question** (ambiguous intent that should be verified)

   Group findings by file. Within each file, order by severity (bug > risk > question).

8. After all files are reviewed, provide a summary:
   - Total files reviewed and total files skipped (if any)
   - Count of findings by severity
   - Which critical paths from the review criteria were covered
   - Any critical paths that could not be reviewed (e.g., referenced files that don't exist)

9. If you find nothing actionable, say so briefly. Do not pad the review with low-confidence observations.

10. After reporting findings, check whether any finding represents a pattern not already covered in docs/review-criteria.md. If so, propose specific additions — a new domain-specific check, a new known fragile area, or a new critical path. Show exactly what you would add and where. Ask for confirmation before editing the file. If there are no new patterns worth capturing, skip this step silently.

11. After step 10, count the total items in docs/review-criteria.md. If the count exceeds 30, mention it briefly: "Criteria doc is at [N] items — consider running /review-criteria-prune to keep it focused." Do not prune or modify anything yourself in this step. If the count is 30 or under, skip this step silently.