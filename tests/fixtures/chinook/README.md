# Chinook SQLite smoke fixture

- Origin: <https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite>
- Upstream version: 1.4.5
- License: MIT; see <https://github.com/lerocha/chinook-database/blob/v1.4.5/LICENSE.md>.
- SHA-256: `bdf635be69850bd3be09c9a2dbeef7ddfb80036bd3ef3381383cd03b61e4a61a`

This unmodified upstream SQLite file is a local smoke fixture only. It checks
the public SQLite adapter and a known `InvoiceLine.InvoiceId → Invoice.InvoiceId`
relationship; it is not a relationship-discovery benchmark.
