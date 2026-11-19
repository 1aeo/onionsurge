# Existing-account source installation

`scripts/install_source.py` installs the query-only gateway under an existing SSH
account's home. It never creates accounts, invokes sudo, edits global SSH policy,
or changes a service. The administrator connection must already be trusted.

Use a complete reviewed commit of this repository containing `scripts/install_source.py`,
`scripts/query_protocol.py`, `scripts/query_gateway.py`, and `catalogue/queries.json`.
The tool reads those exact Git blobs; uncommitted working-tree
changes are not transferred. The dedicated publisher key must be a single
ED25519 public key. Its private key stays on the publisher.

```sh
python3 scripts/install_source.py \
  --admin-target "$SOURCE_ADMIN_ALIAS" \
  --publisher-public-key "$PRIVATE_DIR/publisher.pub" \
  --allowed-address "$CONFIRMED_PUBLISHER_ADDRESS" \
  --attestations "$PRIVATE_DIR/runtime-attestations.json" \
  --commit "$REVIEWED_APPLICATION_COMMIT" \
  --receipt "$PRIVATE_DIR/source-install-receipt.json"
```

`--allowed-address` is a supplied exact, unscoped IP address, not a guessed address,
hostname, wildcard or network range. `--prometheus-port` defaults to the standard
local port and may be supplied explicitly. The optional receipt directory must
already be private and writable. Receipts contain commit/configuration identities
and generic relative paths; stdout omits account, host and address values.

The administrator transport uses strict host-key checking, batch authentication,
and no agent forwarding. The private payload travels over stdin;
private evidence and source restrictions are not embedded in published code.
The actual source interpreter must be `/usr/bin/python3` version 3.11 or later.

Managed paths are fixed relative to the account-database home:

- `.local/lib/onionsurge-source/releases/COMMIT/`: immutable, hash-checked `query_protocol.py`, `query_gateway.py`, and `queries.json`.
- `.config/onionsurge-source/generations/DIGEST/`: an atomic private generation
  containing the matching catalogue, gateway settings and attestation file.
- `.local/state/onionsurge-source/`: gateway/installation locks and private
  backups of the authorization file before each change.

Managed directories/files must be owned, nonsymlink paths. Intermediate existing
directories may be readable by others but cannot be group/world writable; managed
roots and files are private. Existing generations are reused only when every
byte and file name matches. Previous generations remain available for inspection.
All installers use one account lock; do not edit the managed trees or
`authorized_keys` concurrently through another tool.

The existing `.ssh` directory must have mode 0700 and its existing
`authorized_keys` must be an owned regular file with mode 0600. Only entries for
the supplied public key are replaced or deduplicated. All other lines, including
comments and unrelated keys, are preserved. A private backup is made first; a
detected concurrent file change aborts before replacing it. Gateway self-checks
run before the key is changed. A failure can leave an unused private generation,
but does not rewrite other authorization entries or remove prior generations.

The dedicated entry uses `restrict`, the supplied exact `from=` restriction,
and a fixed command. That command runs `env -i` with only a fixed `PATH` and the
quoted `SSH_ORIGINAL_COMMAND`, then `/usr/bin/python3 -I -B` and the pinned script,
configuration and state paths. The original command is data, not shell code.
Bytecode writes are disabled so the pinned code directory remains unchanged.

After installation, verify the dedicated publisher key can list the fixed
catalogue and request a bounded aggregate, while shell and forwarding requests
are refused. Keep the existing administrator route available for correction.
This installer does not infer other global account authorization policy.

Private daemon evidence is a source-installation input through `--attestations`.
It is not a publisher environment setting and is never downloaded by the
collector. Refresh it with independently reviewed observations and reinstall a
new immutable configuration generation; unchanged process checks do not renew a
daemon observation's timestamp. See [query-gateway.md](query-gateway.md).
