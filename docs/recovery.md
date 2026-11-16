# Recovery commands

Use the **same export, destination, and journal**. The CLI saves progress after each confirmed operation. `status` reads local state; `verify` checks remote contents. Run `migrator COMMAND --help` for options.

## Uncertain playlist creation

Resume first. It searches for the persisted marker in destination descriptions and reuses exactly one matching playlist. If no unique match is found, inspect the destination. Never create another copy while the original request may still complete.

Once resolved, adopt an inspected **empty private** destination by ID:

```sh
docker compose run --rm migrator map-playlist SOURCE_ID DESTINATION_ID
```

For an unlisted migration, add `--playlist-privacy unlisted`. Existing mappings cannot be overwritten.

## Uncertain video insertion

Resume first: it acknowledges an exact matching pending occurrence without inserting again. If the result remains ambiguous, wait and inspect the destination's entire sequence. Only after confirming the original request can no longer commit:

```sh
docker compose run --rm migrator resolve-pending SOURCE_ID applied
# Or use absent if the insertion definitely did not happen.
docker compose run --rm migrator resume
```

The selected outcome must match the remote sequence. Failed/unavailable entries are not automatically appended later, which would change their position. Verification reports them; any manual repair must use the correct source position.

## Other options

- `skip-subscription CHANNEL_ID`: record a deliberate local omission. Verification reports it as skipped.
- `resume --continue-on-conflict`: continue unrelated work while leaving conflicts unresolved.
- `check-subscription`: preview one missing source subscription; `--apply` attempts one actual write.
- `--state data/state/OTHER.sqlite3`: select an intentionally separate migration journal.

Do not switch journals to hide a mismatch. Re-exporting changes the generation; restore the original export when resuming old progress. No-channel destination identity uses a hash of its saved OAuth refresh credential, so changing credentials can also invalidate the binding. No automated recovery command deletes remote content.
