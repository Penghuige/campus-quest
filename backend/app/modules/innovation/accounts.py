"""The raw ``users`` row shape the innovation module locks and reads.

The identity ORM models stay behind the directory port (importlinter
contract), so module code that needs the account row — talent locks,
public visibility queries, project-draft guards — reads this bare
table instead. Three columns cover every use: the UUID identity, the
role, and the status.
"""

from sqlalchemy import String, Uuid, column, table

USERS = table(
    "users", column("id", Uuid), column("role", String), column("status", String)
)
