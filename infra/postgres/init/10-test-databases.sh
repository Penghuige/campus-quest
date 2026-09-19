#!/bin/bash
# Creates the dedicated integration-test role and database. The application
# database (campusquest, owned by POSTGRES_USER) is created by the postgres
# image itself from POSTGRES_DB; this script only adds the test access path
# used by backend/tests/integration (see db_guard.py).
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE test WITH LOGIN PASSWORD 'test';
    CREATE DATABASE campusquest_test OWNER test;
EOSQL
