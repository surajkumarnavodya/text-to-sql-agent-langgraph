#!/bin/sh
# Postgres official image init script (docker-entrypoint-initdb.d/) --
# creates the *second* database this stack needs. `POSTGRES_DB` (set in
# docker-compose.loadtest.yml) provisions `loadtest_business` automatically
# on first boot; identity/__init__.py's "never the same database as the
# business connections" rule means the identity database
# (`loadtest_identity`) needs its own, created here. Runs before
# `01-schema.sql` (alphabetical order), which applies against the default
# `POSTGRES_DB` connection.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE loadtest_identity;
EOSQL
