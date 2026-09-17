# Release identity region, version 2

A released executable carries its release identity in one fixed-size region that
the executable reads about itself at startup. The build reserves the region
empty; distribution binds it after the build, without recompiling, so one built
workload can carry different release markers.

This document is the only definition of the format. Writers and readers
implement it and test against `fixtures/`.

## Placement

| Object format | Section |
| --- | --- |
| ELF, PE | `.releaseid` |
| Mach-O | segment `__DATA`, section `__releaseid` |

An executable has exactly one such section, exactly 4096 bytes long, with file
content and no relocations. A PE input that already carries an Authenticode
signature is refused.

## Layout

All offsets are bytes from the start of the region. Text fields are UTF-8,
zero-padded, and must not contain bytes after their first zero.

| Offset | Length | Field |
| --- | --- | --- |
| 0 | 16 | magic, the ASCII bytes `RELEASE.IDENT.V2` |
| 16 | 64 | origin prefix: uppercase ASCII letters and `_`, non-empty |
| 80 | 40 | origin commit: empty, or 40 lowercase hex digits |
| 120 | 128 | origin target: the target triple the build was made for |
| 248 | 8 | payload length, unsigned little-endian |
| 256 | 32 | SHA-256 of the whole region with these 32 bytes omitted |
| 288 | 3808 | payload, zero-padded |

The origin fields are written at build time. Payload length, checksum and payload
are written only by binding.

## Unbound and bound

An **unbound** region has payload length 0 and every byte from 256 onward zero.
Its checksum is not checked.

A **bound** region has a payload length between 1 and 3808, zero bytes after the
payload, a matching checksum, and a payload that is a JSON object with exactly
these string fields:

| Field | Rule |
| --- | --- |
| `product` | uppercased with `-` replaced by `_`, equals the origin prefix |
| `marker` | `v<major>.<minor>.<patch>` optionally followed by `-alpha.N`, `-beta.N` or `-rc.N` with N ≥ 1 |
| `digest` | 64 lowercase hex digits |
| `commit` | 40 lowercase hex digits |
| `workload` | 64 lowercase hex digits |

Writers serialize the payload compactly in the field order above. Readers must
not depend on field order.

Binding an already bound region succeeds only when the payload is identical, and
then changes nothing. Every other deviation is refused.

## Build-time conventions

A product whose origin prefix is `P` builds with:

| Variable | Meaning |
| --- | --- |
| `P_BUILD_TARGET` | origin target |
| `P_BUILD_COMMIT` | origin commit, optional |
| `P_BUILD_CHANNEL` | `unbound` for distributable builds; such an executable refuses to run commands until bound |

## Fixtures

`fixtures/manifest.json` lists each region file with the outcome a reader must
produce: the decoded origin and binding, or a refusal.
