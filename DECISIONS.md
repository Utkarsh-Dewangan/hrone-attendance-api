# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.** We created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce business uniqueness and prevent write races. We added compound indexes on `attendance_logs.(date, emp_code)` for sorted pagination and `employees.(department, joined_on)` for headcount filtering. We considered and rejected an index on `(status, date)` because date-leading compound indexes already provide efficient `IXSCAN` coverage.

2. **Punch-in race.** Both concurrent requests resolve to the same attendance date and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` atomically commits the first write. The second request triggers a `DuplicateKeyError`, which our exception handler intercepts to immediately return HTTP 409.

3. **Ties.** We apply MongoDB's `$setWindowFields` with standard competition ranking (`$rank`), so ties share a rank (e.g., 1, 2, 2, 4). The `limit` filter runs after ranking (`rank <= limit`). Both tied employees at the cutoff are returned, even if total rows exceed `limit`. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.** The aggregation pipeline starts from `employees` (matching `joined_on <= period_end`), grouping by department to establish baseline headcount before using `$lookup` to join attendance records. Employees with zero logs remain counted.

5. **100x Data Change.** I would introduce pre-aggregated daily department rollups updated via MongoDB change streams or nightly worker jobs, avoiding real-time cross-collection joins and `$densify` stages across millions of documents.
