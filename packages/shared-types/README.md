# @localapply/shared-types

The agent protocol, shared between the Python services and the TypeScript apps.

**`services/api/localapply/contracts.py` is the source of truth.** JSON Schema here is
generated from it, not written by hand — two hand-maintained copies of a contract drift, and
the drift shows up as a runtime bug in the dashboard rather than a compile error.

```bash
# regenerate the schemas
cd services/api && python scripts/export_schemas.py

# generate TypeScript from them
npx json-schema-to-typescript -i packages/shared-types/schemas -o apps/web/src/generated
```

Until that generation step is wired into the build, `apps/web/src/types.ts` mirrors
`contracts.py` by hand and must be updated alongside it.
