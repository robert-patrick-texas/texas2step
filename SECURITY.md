# Security policy

## Reporting a vulnerability

Please don't put the details of a security problem in a public issue.
[Report it privately](https://github.com/robert-patrick-texas/texas2step/security/advisories/new)
on GitHub instead, from the repository's Security tab ("Report a vulnerability"). Only
the maintainers see the report, and the fix and its advisory are prepared with you
there before anything is made public.

## Supported versions

Security fixes come in a new release, made from the newest one. Keep servers on the
newest release: `sudo texas2step update` installs it, and `texas2step-update.timer` can
do that unattended (see [Upgrade](docs/install.md#upgrade)).

## The controls

What texas2step does to protect a server and its visitors, what each control guards
against, and what is left to the operator: [Security](docs/security.md).
