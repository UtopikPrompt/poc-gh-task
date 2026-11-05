"""PostgreSQL connection URL construction for the LangGraph service."""

from __future__ import annotations

import os

from sqlalchemy.engine import URL


def postgres_url_from_environment(service: str, *, required: bool) -> str | None:
    fields = ("HOST", "PORT", "NAME", "USERNAME", "PASSWORD", "SSLMODE")
    values = {field: os.environ.get(f"{service}_DB_{field}", "") for field in fields}
    if not required and not any(values.values()):
        return None

    missing = [f"{service}_DB_{field}" for field, value in values.items() if not value]
    if missing:
        raise ValueError(f"Database settings are missing: {', '.join(missing)}")

    try:
        port = int(values["PORT"])
    except ValueError as error:
        raise ValueError(f"{service}_DB_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"{service}_DB_PORT must be between 1 and 65535")

    return URL.create(
        drivername="postgresql",
        username=values["USERNAME"],
        password=values["PASSWORD"],
        host=values["HOST"],
        port=port,
        database=values["NAME"],
        query={"sslmode": values["SSLMODE"]},
    ).render_as_string(hide_password=False)
