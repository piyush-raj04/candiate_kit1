# DECISIONS.md

1. **Indexes.** A unique `employees.emp_code` index protects business identity under concurrent creates. A unique `(emp_code, date)` index makes duplicate punch-ins impossible even when requests race. Employee department/join-date and attendance date/employee, employee/date/status, status/date, and date/late-minute indexes support common filters, date ranges, and sorts. I would verify the explain plans on the grader dataset and tune compound index order to the most selective filters. I avoided a large index for every possible combination because each index slows writes and consumes storage.

2. **Punch-in race.** Both requests may check the employee and calculate the same attendance date. Both then try to insert. MongoDB's unique `(emp_code, date)` index allows only one insert. The successful request returns 201; the other catches `DuplicateKeyError` and returns 409.

3. **Ties.** The leaderboard assigns standard competition ranks: equal totals share the same rank and the next rank is skipped. It filters on `rank <= limit`, so ties at the cutoff are all returned. Rows are then ordered by total minutes descending and employee code ascending.

4. **Headcount.** The department summary starts from employees whose `joined_on` is on or before the month's last day, then associates the month's attendance records. Employees without logs remain in the employee set and count toward headcount.

5. **100x scale.** I would move each analytics calculation into well-indexed MongoDB aggregation pipelines, inspect `executionStats`, and consider pre-aggregated daily summaries if the query latency or resource use requires it. This reduces application memory use and avoids transferring large result sets to Python.
