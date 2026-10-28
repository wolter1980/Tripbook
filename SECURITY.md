# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Report them privately through this
repository's **Security** tab → **Report a vulnerability**. Only the maintainer can see the report.

Describe the problem, how to reproduce it and what an attacker could do with it. You'll get an answer
as soon as possible; once a fix is released, you're credited in the changelog if you wish.

## Supported versions

Only the latest release on `main` receives fixes.

## Scope

Tripbook is a desktop application that keeps its data locally. Relevant problems include, for example:
files (tracker dumps, GPX, Little LogBook databases, backups) that make Tripbook execute code or write
outside its data folder, and anything that sends logbook data off the computer unexpectedly. Tripbook
contacts the internet only for map tiles and, on request, OpenStreetMap address and place-name lookups.
