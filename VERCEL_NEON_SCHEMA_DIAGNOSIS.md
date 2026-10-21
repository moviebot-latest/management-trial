# Vercel / Neon database diagnosis (non-destructive)

The latest production log says the existing `user` table is missing `id` and
`department`. The application deliberately stops rather than guessing a migration,
because changing a user table without knowing its real columns can lock existing
accounts out.

## 1. Verify the database URL
In Vercel > Project Settings > Environment Variables, check `DATABASE_URL`
(or `SQLALCHEMY_DATABASE_URI`, depending on the app configuration). It must point
to the existing Neon database that contains the application's real employee/user data.
Do not paste the secret URL into chat and do not reset or recreate the database.

## 2. Inspect the actual table columns
In Neon SQL Editor, run this read-only query:

```sql
SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND table_name IN ('user', 'users')
ORDER BY table_schema, table_name, ordinal_position;
```

Also inspect the available tables:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND table_type = 'BASE TABLE'
ORDER BY table_schema, table_name;
```

Share the result (hide personal data and all credentials). The safe next step depends
on the actual schema. Do not add a made-up `id` primary key or drop/rename tables
without a backup and a verified mapping.
