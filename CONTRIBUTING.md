# Contributing

Thanks for helping. The most useful contributions, roughly in order:

1. **Compatibility reports.** Tell us which camera models work, partly work or
   don't. Use the *Camera compatibility* issue template.
2. **Bug reports** with redacted diagnostics and the output of
   `scripts/verify_remote_stream.py`, which separates "the cloud path doesn't
   work for me" from "the integration mishandles it".
3. **Pull requests** for fixes and features.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-test.txt
ruff check . && pytest
```

The tests need no network, account or camera: the cloud is mocked and the relay
is a scripted fake. Please keep it that way. A test that needs real credentials
can't run in CI and risks leaking them.

## Guidelines

- **Keep the transport separate.** `api.py`, `parsers.py` and `relay_stream.py`
  must not import Home Assistant. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Touch OnTapo internals in one place.** Private OnTapo helpers are reused only
  in `relay_stream.py` and the MFA helpers in `api.py`, and are guarded by
  `assert_ontapo_compatible()`.
- **Add a test** for any behaviour change.
- **Never commit** credentials, tokens, verification codes, camera footage,
  snapshots or real device ids. `.gitignore` excludes the common file types,
  but check your diff.
