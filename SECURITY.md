# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report it privately through GitHub's
[security advisory form](https://github.com/Limmi1/ha-tapo-cloud/security/advisories/new).
You'll get an acknowledgement within a few days, and a fix or a clear answer
as soon as practical.

## What's in scope

- Credential or token handling in the integration, including anything that
  could write a password or token to disk, logs or diagnostics.
- Ways for another user or device on the network to view a camera through
  this integration.
- Unsafe handling of data received from the cloud relay.

TP-Link's own cloud service is out of scope. Report issues there to TP-Link.

## How credentials are handled

- The account password is used only during sign-in and the two-factor code
  request, then discarded. It is never written to the config entry.
- The session and refresh tokens are stored in Home Assistant's config entry,
  as for any cloud integration. They grant access to every camera on the
  account until they expire.
- Tokens are never logged at any level. Diagnostics redact the token, refresh
  token, email address, terminal id and device ids.

## Never include in an issue

Passwords, tokens, verification codes, camera footage or snapshots, or
unredacted diagnostics.
