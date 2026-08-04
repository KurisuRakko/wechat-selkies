# Third-party notices

This integration adapts the Linux process scanner, SQLCipher 4 page
decryption, HMAC validation, WAL handling, and message decoding approach from
`huohuoer/wechat-cli` version 0.2.4 at commit
`a3789232d4f79bf0b30634d9dadbce71e4acd601`.

The adapted files carry comments describing the changes. The local
implementation additionally:

- restricts all database access to one fixed account and a DB allowlist;
- removes raw key, salt, address, and path logging;
- uses chunked process-memory reads;
- stores secrets in a `0700` directory with mode `0600` files;
- keeps decrypted databases only in a 512 MiB tmpfs;
- validates every SQLCipher page HMAC and committed WAL boundary;
- exposes a private stdio MCP server with no send-message operation.

The upstream work is licensed under Apache License 2.0. A copy is included as
`LICENSE.wechat-cli`.
