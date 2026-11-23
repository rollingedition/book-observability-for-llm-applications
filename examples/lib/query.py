"""DuckDB over the rows a receiver stores (plan section 6, chapter 5).

`connect(rows_path)` reads a JSON Lines file written by `export.write_jsonl` and gives one view, `spans`, with the
columns of the rows plus `service`, the resource's `service.name`. The later chapters' queries read this view. Every
query that prints needs an ORDER BY with a tie-break (rule 14, K-7): DuckDB may return rows in another order when the
table is large, and a query that prints must not depend on that.
"""
import duckdb

# The sequence numbers are left out: the contacts server's IDs are random 128-bit and 64-bit numbers, which no integer
# column holds, and the queries join on the hex IDs.
_COLUMNS = {
    "trace_id": "VARCHAR", "span_id": "VARCHAR",
    "parent_span_id": "VARCHAR", "name": "VARCHAR", "kind": "VARCHAR", "start_ns": "BIGINT", "end_ns": "BIGINT",
    "status": "VARCHAR", "attributes": "JSON", "events": "JSON", "resource": "JSON", "scope": "VARCHAR",
}


def connect(rows_path):
    """Return an in-memory DuckDB connection whose view `spans` reads the rows at `rows_path`.

    Why the column types are named: with them, DuckDB does not infer a type from the first rows it sees, so an
    attribute that is a number in one span and a string in another reads the same in every run.
    """
    columns = ", ".join(f"'{name}': '{kind}'" for name, kind in _COLUMNS.items())
    path = str(rows_path).replace("'", "''")
    connection = duckdb.connect()
    connection.execute(
        "CREATE VIEW spans AS SELECT *, json_extract_string(resource, '$.\"service.name\"') AS service "
        f"FROM read_json('{path}', format='newline_delimited', columns={{{columns}}})"
    )
    return connection
