# Logos catalog format: `logos-repo.json` and `index.json`

This is the reference for the two JSON files that define a Logos module
catalog:

- **`logos-repo.json`** — the catalog's *identity card*. One per catalog.
  Small, hand-edited, served at a stable URL. It tells a client what the
  catalog is called, who's trusted to sign its packages, and — crucially —
  where to find the actual package listing.
- **`index.json`** — the catalog's *package listing*. Generated (by
  [`index.py`](../index.py) or the GitHub-Actions `rebuild-index.yml`),
  never hand-edited. It enumerates every package, every version, and
  everything a client needs to download and verify each `.lgx`.

Both are plain UTF-8 JSON. This document is the contract between catalog
publishers and the clients that consume catalogs — `lgpd`, the
`package_downloader` module, and the package-manager UI.

> **Authoritative sources.** The consumer side is
> `logos-package-downloader/src/package_downloader_lib.cpp`
> (`parseLogosRepoJson`, `getCatalogJson`, `verifyDownloadAgainstIndex`).
> The producer side is this repo's [`index.py`](../index.py). Where this
> document and the code disagree, the code wins — please file a fix.

---

## Table of contents

1. [How the pieces fit together](#1-how-the-pieces-fit-together)
2. [`logos-repo.json`](#2-logos-repojson)
3. [`index.json`](#3-indexjson)
4. [The embedded `manifest` object](#4-the-embedded-manifest-object)
5. [The embedded `signature` object](#5-the-embedded-signature-object)
6. [Version ordering and selection](#6-version-ordering-and-selection)
7. [The download verification contract](#7-the-download-verification-contract)
8. [Fields the client synthesises at read time](#8-fields-the-client-synthesises-at-read-time)
9. [Schema versioning and compatibility](#9-schema-versioning-and-compatibility)
10. [Producing and validating an index](#10-producing-and-validating-an-index)

---

## 1. How the pieces fit together

A client installs a package by walking this chain:

```
                          (1) client is configured with a repo URL
                              ── the URL of a logos-repo.json ──
                                          │
                                          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  logos-repo.json   (identity + pointers)                     │
   │    name, displayName, trustedSigners[], indexUrl ───────┐    │
   └─────────────────────────────────────────────────────────┼────┘
                                          │ (2) fetch indexUrl │
                                          ▼                    │
   ┌─────────────────────────────────────────────────────────────┐
   │  index.json   (the package listing)                          │
   │    packages[].versions[].{ url, rootHash, manifest, … } ─┐   │
   └──────────────────────────────────────────────────────────┼───┘
                                          │ (3) fetch a version's url
                                          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  <package>.lgx   (the actual artifact)                       │
   │    verified against the index entry before install (§7)      │
   └─────────────────────────────────────────────────────────────┘
```

1. A client is pointed at a **`logos-repo.json` URL** (the built-in default,
   or one a user added). Multiple repos can be configured at once; the
   client merges them into a single catalog.
2. The client reads `indexUrl` from that file and fetches the
   **`index.json`** it points to.
3. To install a package the client picks a version, downloads the `.lgx`
   from that version's `url`, and **verifies the file against the index
   entry** (§7) before handing it to the installer.

The two files live at different cadences and are produced by different
hands:

| | `logos-repo.json` | `index.json` |
|---|---|---|
| **Edited by** | a human, rarely | a tool, on every release |
| **Count** | one per catalog | one per catalog |
| **Typically served at** | a stable raw URL (e.g. `…/HEAD/logos-repo.json`) | a release asset / object-store URL (the `indexUrl`) |
| **Hand-editing** | expected | never — regenerate instead |

`indexUrl` is deliberately a *separate* URL from `logos-repo.json`. The
identity card changes almost never and can sit in a git repo's raw view;
the index changes on every publish and is better served from wherever the
`.lgx` files already live.

---

## 2. `logos-repo.json`

The catalog's identity card. Fetched first; everything else is reached
through it.

### 2.1 Example

```json
{
  "schemaVersion": 1,
  "name": "logos-modules-official",
  "displayName": "Logos Official Modules",
  "description": "The modules maintained by the Logos core team.",
  "homepage": "https://github.com/logos-co/logos-modules-v2",
  "indexUrl": "https://github.com/logos-co/logos-modules-v2/releases/download/index/index.json",
  "network": "logos.test",
  "trustedSigners": [
    {
      "did": "did:jwk:eyJjcnYiOiJFZDI1NTE5Iiwia3R5IjoiT0tQIiwieCI6Ii4uLiJ9",
      "name": "Logos Core Team"
    }
  ]
}
```

### 2.2 Field reference

| Field | Type | Required | Meaning |
|---|---|:--:|---|
| `name` | string | **yes** | Canonical, stable identifier for the catalog. Used as a key (e.g. to scope a download to one repo) and shown when no `displayName` is available. Keep it short, lowercase, and stable — renaming it is effectively a new catalog. |
| `displayName` | string | **yes** | Human-facing label shown in the UI (e.g. repository list). Free-form. |
| `indexUrl` | string | **yes** | Absolute URL of this catalog's `index.json`. The client fetches it verbatim — point it at wherever you publish the index. |
| `description` | string | no | One-line human description. Defaults to empty. |
| `homepage` | string | no | Informational URL (project page, docs). Defaults to empty. |
| `network` | string | no | Logos Storage network. The catalog publishes its packages to this network. Defaults to empty. |
| `trustedSigners` | array | no | Signer identities this catalog vouches for. See §2.3. Defaults to empty. |
| `schemaVersion` | number | no* | Format version. See §2.4. |

A client that can't fetch or parse `logos-repo.json`, or that is missing
any of the three required fields (`name`, `displayName`, `indexUrl`),
records a per-repo `resolveError` and drops the repo from the merged
catalog — it does not abort. Other configured repos still work.

### 2.3 `trustedSigners`

An array of signer objects. Each object:

| Field | Type | Required | Meaning |
|---|---|:--:|---|
| `did` | string | **yes** | The signer's decentralised identifier (`did:jwk:…`). This is the only field the client reads — it's the key used to decide whether a signed package is trusted. |
| `name` | string | no | Human label for the signer. Convention only — **the client ignores it**. Include it anyway so the file is self-documenting. |

Objects without a string `did` are skipped. An empty or absent
`trustedSigners` means the catalog vouches for no signer; whether an
unsigned or untrusted-signer package is then installable is a *client
policy* decision (e.g. the package-manager's signature-policy setting),
not something this file controls.

> The DID here must match the `did` embedded in a package's signature
> (§5). That's how trust flows end to end: the catalog lists a DID as
> trusted, the `.lgx` is signed by that DID, and the client checks the
> chain at install time.

### 2.4 `schemaVersion`

`schemaVersion` is **advisory** for `logos-repo.json`: the current client
parser does not read or enforce it, and tooling that writes the file emits
`1`. Include `"schemaVersion": 1` anyway — it future-proofs the file
against a day when the client *does* gate on it, and it documents intent.
Treat a bump as a signal that required fields or their meaning changed.

---

## 3. `index.json`

The package listing. Generated, never hand-edited. One file lists every
package in the catalog and every published version of each.

### 3.1 Example

```json
{
  "schemaVersion": 2,
  "repositoryName": "logos-modules-official",
  "generatedAt": "2026-05-22T01:46:54Z",
  "packages": [
    {
      "name": "wallet_module",
      "versions": [
        {
          "releasedAt": "2026-05-12T13:36:54Z",
          "publisherRef": "wallet_module-v1.0.0",
          "url": "https://github.com/logos-co/logos-modules-v2/releases/download/wallet_module-v1.0.0/wallet_module-1.0.0.lgx",
          "urls": [
            "logos:zDvZRwzm3g3mPcYu1NmDKV5jCccw4FZ83XKyu85AjSCg7gH7zQdL",
            "https://github.com/logos-co/logos-modules-v2/releases/download/wallet_module-v1.0.0/wallet_module-1.0.0.lgx"
          ],
          "size": 17083080,
          "sha256": "9ab4be725b4b669713ca39f9f29648b5f26e41c89ea1a1bd7b1472db0d9ebb47",
          "rootHash": "ccf6b318787e6ffc33ce38a3940c0cf29334c2787a777eb036d29a0c1214858b",
          "manifest": {
            "manifestVersion": "0.1.0",
            "name": "wallet_module",
            "version": "1.0.0",
            "type": "core",
            "category": "wallet",
            "author": "Logos Core Team",
            "description": "Wallet module using go-wallet-sdk C library",
            "icon": "wallet.png",
            "dependencies": [],
            "main": {
              "darwin-arm64": "wallet_module_plugin.dylib",
              "linux-amd64": "wallet_module_plugin.so",
              "linux-arm64": "wallet_module_plugin.so"
            },
            "hashes": {
              "root": "ccf6b318787e6ffc33ce38a3940c0cf29334c2787a777eb036d29a0c1214858b",
              "variants": "29591a2180d6bc167a9d019ee4dad15797ead738b5198698830804d68e6141be",
              "variants/darwin-arm64": "c1141f86a24bd39ae1bacaac9d8152d20928993526a9d55a1f1ec77099f8dcb8",
              "variants/linux-amd64": "7fdb640843390c72074dc6f5e54f89da365d649f513bafdf2b0e7dd702dac1a2",
              "variants/linux-arm64": "3b30935c5ba3175fee321ffdb58fbda9e5c09f7c3776d6ec02245c60a2634420"
            }
          },
          "signature": {
            "algorithm": "ed25519",
            "did": "did:jwk:eyJjcnYiOiJFZDI1NTE5Iiwia3R5IjoiT0tQIiwieCI6Ii4uLiJ9",
            "signature": "J7URtd3EjXvyMSSCbwK20vJUmXzaEIQqLkodASbVQgi…",
            "signer": { "name": "Logos Core Team" },
            "linkedDids": [],
            "version": 1
          }
        }
      ]
    }
  ]
}
```

### 3.2 Top-level fields

| Field | Type | Required | Meaning |
|---|---|:--:|---|
| `schemaVersion` | number | **yes** | Index format version. Currently **`2`**. See §9. |
| `repositoryName` | string | **yes** | Catalog identifier. Should equal the `name` in the catalog's `logos-repo.json`. Informational on the consumer side (the client trusts the repo it fetched the index *from*), but keep them in sync. |
| `generatedAt` | string | **yes** | ISO-8601 UTC timestamp, second precision, trailing `Z` (e.g. `2026-05-22T01:46:54Z`). When this index was built. Bumped on every regeneration. |
| `packages` | array | **yes** | The package listing. May be empty (`[]`) for a catalog with nothing published yet. |

### 3.3 `packages[]` — one entry per package

| Field | Type | Required | Meaning |
|---|---|:--:|---|
| `name` | string | **yes** | The package / module name. Matches the `name` in every embedded manifest of its versions. Entries without a `name` are skipped by the client. |
| `versions` | array | **yes** | All published versions of this package, newest first. See §3.4. A package with no versions is meaningless — don't emit one. |

### 3.4 `versions[]` — one entry per published version

Every field a client needs to **fetch and verify** one specific build of a
package lives here. The array is sorted **descending by `releasedAt`**
(§6).

| Field | Type | Required | Role | Meaning |
|---|---|:--:|---|---|
| `url` | string | **yes** | download | Absolute URL of the `.lgx` artifact. The client GETs this to install. |
| `rootHash` | string | **yes** | **binding** | Hex SHA-256 Merkle root of the package content. Must equal the `.lgx`'s own `manifest.hashes.root`. The strongest index↔file binding (§7). |
| `manifest` | object | **yes** | **binding** + display | A verbatim copy of the `.lgx`'s embedded `manifest.json` (§4). The client reads display fields from it and binds a subset against the downloaded file (§7). |
| `releasedAt` | string | **yes** | ordering | ISO-8601 UTC `Z` timestamp. Drives version ordering and "newest" selection (§6). Missing/empty sorts *last*. |
| `urls` | array | no | download | A list of sources for this artifact, one string per source. Schemes currently supported: `https://` and `logos:`. |
| `signature` | object | no | **binding** | A verbatim copy of the `.lgx`'s `manifest.sig`, when the package is signed (§5). **Omit the key entirely for unsigned packages** — do not emit `null`. |
| `publisherRef` | string | no | informational | Provenance tag, conventionally `<name>-v<version>` (the release tag in the GitHub flow). The client ignores it. |
| `size` | number | no | informational | Size of the `.lgx` in bytes. Display / progress only. **Not** part of the client's download-verification (§7). |
| `sha256` | string | no | informational | Hex SHA-256 of the whole `.lgx` *file*. Display / external integrity checks. **Not** checked by the client's install-time verification — which binds on `rootHash` instead (a content hash, robust to archive-level repackaging). |

**Required vs. optional, in one sentence:** a version entry is usable by
the client iff it has `url`, `rootHash`, `releasedAt`, and a `manifest`
with the binding fields of §4; everything else is informational or
signed-only.

> **`rootHash` vs `sha256` — why both?** `sha256` fingerprints the exact
> bytes of the `.lgx` file; `rootHash` fingerprints the *content* (the
> Merkle root over the manifest + variant trees, recomputed by `lgx` on
> every change). The client binds on `rootHash` because it survives benign
> repackaging of the archive and is what the manifest itself records, so
> the index and the file can be cross-checked without trusting the
> transport. `sha256` is kept for humans and out-of-band integrity tools.

### 3.5 `urls` — one entry per artifact source

`url` stays required for backwards compatibility. It is used as the fallback source when `urls` is absent
or does not contain any `https://` scheme.

`urls` lists the available sources, in no particular order:

| Scheme | Meaning |
|---|---|
| `logos:<cid>` | Logos Storage content identifier. Fetched through a storage node, which must run on the network the repository declares (§2.2). The CID is derived from the content, so the same artifact has the same CID on every network. |
| `https://…` | A plain HTTPS mirror, equivalent to `url`. |

Every source must serve the same content.

---

## 4. The embedded `manifest` object

`versions[].manifest` is a byte-faithful copy of the `manifest.json` inside
the `.lgx`. (`index.py` obtains it via `lgx manifest <pkg> --json`, which
emits the embedded bytes verbatim.) Its fields:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Package/module name. **Binding** (§7) — must match the file. Also must match the enclosing `packages[].name`. |
| `version` | string | Package version (e.g. `1.0.0`). **Binding**. Combined with `rootHash` it's the dedup key for a version entry. |
| `type` | string | Package kind, e.g. `core` (a backend module plugin) or `ui_qml` (a QML UI plugin). **Binding** — a `core`↔`ui_qml` swap is load-path-relevant. |
| `main` | object | Map of `variant → entry-point relative path`, e.g. `{ "linux-amd64": "foo_plugin.so" }`. The set of keys is the set of platforms this build supports. **Binding**. |
| `dependencies` | array | Other packages this one needs. Each element is either a bare string (`"waku_module"`) or an object `{ "name", "version"?, "signer"? }` where `version` is an npm/Cargo-style range and `signer` pins a DID. **Binding** — the transitive surface. |
| `hashes` | object | Merkle tree, hex SHA-256 per subtree. Keys: `root` (the whole package), `variants`, `variants/<name>`, and optionally `docs` / `licenses`. `hashes.root` is the value that must equal the version entry's `rootHash`. |
| `manifestVersion` | string | Version of the *manifest schema itself* (e.g. `0.2.0`) — distinct from the package `version`. |
| `category` | string | Grouping label for the UI (e.g. `wallet`, `chat`). Display only. |
| `author` | string | Free-form author/team string. Display only. |
| `description` | string | One-line package description. Display only. The client lifts this (plus `type`/`category`/`author`/`icon`) from the **first** version's manifest to describe the package as a whole. |
| `icon` | string | Icon file name within the package, or empty. Display only. |
| `view` | string | For `ui_qml` packages: the QML entry point relative to the variant root. Empty for other types. |
| `provides` | array | Intent **names** this package can service: `[{"intent": "chat.group.open"}]`. Present from `manifestVersion` 0.5.0. Always objects — the bundler normalises a bare string **up** to `{"intent": ...}` — so a reader never sees two shapes, and a future field costs no shape change. The author's `params` (the payload shape an intent expects) is deliberately **not** carried: the shell enforces that against the installed `metadata.json`, so a copy here would be a snapshot nothing reads. Display only **today** — see the note below. |

The five **binding** fields — `name`, `version`, `main`, `dependencies`,
`type` — are the ones the client re-checks against the downloaded file
(§7). The rest are display metadata.

> **`provides` and trust.** It is display metadata here, but it is inside the
> manifest, so it *is* covered by the package signature (§5) — the signer
> attests to what the package claims it can do. A shell must still read the
> installed package's own declaration before dispatching an intent to it; the
> catalog copy exists to answer "which installable package provides X?", which
> is a question about what you could install, not about what you may run.
>
> It is deliberately **not** binding yet: a mismatch between the catalog and
> the installed package is a stale index, not an escalation, because nothing
> dispatches on the catalog copy. Promote it to binding (§7) once something
> does.

---

## 5. The embedded `signature` object

Present only for signed packages. `versions[].signature` is a verbatim copy
of the `.lgx`'s `manifest.sig`. (`index.py` obtains it via `lgx signature
<pkg>`; an unsigned package yields no output and the `signature` key is
omitted.)

| Field | Type | Meaning |
|---|---|---|
| `did` | string | The signer's DID (`did:jwk:…`). **The field the client binds on** (§7) and the one matched against the catalog's `trustedSigners` (§2.3). |
| `algorithm` | string | Signature algorithm, e.g. `ed25519`. |
| `signature` | string | Base64 Ed25519 signature over the manifest bytes. |
| `signer` | object | Self-asserted signer metadata, e.g. `{ "name": "…", "url": "…" }`. Informational — never a basis for trust. |
| `linkedDids` | array | Additional DIDs linked to the signer. Informational for the index contract. |
| `version` | number | Signature-envelope schema version. |

**Omit `signature` entirely for unsigned packages.** A `null` value is
tolerated by the consumer's null-safe reads but is non-idiomatic — the
generator never writes it.

---

## 6. Version ordering and selection

`versions[]` is stored **newest-first by [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html)
precedence**, with `releasedAt` breaking ties between entries that share a
version. Both the generator (`index.py` re-sorts on every `build`/`add`) and
the client (`getCatalogJson` stable-sorts on read) enforce this, so a
hand-mangled order is corrected at read time — but keep the file sorted so it
reads correctly raw.

Ordering is computed by `lgx semver`, which is the single implementation the
C++ clients (`lgpm`, `lgpd`, the package-manager UI) also use. The catalog
therefore cannot disagree with them about which version is newest.

> **This used to be "sorted descending by `releasedAt`", and that was a bug.**
> A publish time is not a version. A `1.2.1` backported after `2.0.0` shipped
> put the *lower* version at `versions[0]` — advertised to every client as the
> latest release; publishing `2.0.0-alpha` after `2.0.0` did the same, since
> the pre-release ranks below its own release but carried the newer timestamp.
> A forced republish had the same effect (it refreshes the asset's
> `Last-Modified`, which is what `releasedAt` records). The divergence only
> appears when semver and the timestamp disagree — `2.0.0-alpha` published
> after `1.9.0` lands first under *both* orderings, because `2.0.0-alpha`
> genuinely outranks `1.9.0`.

Precedence follows the spec: a pre-release ranks below its own release
(`1.0.0-rc.1` < `1.0.0`), numeric pre-release identifiers compare *numerically*
(`1.0.0-rc.2` < `1.0.0-rc.11`), and build metadata is ignored. A version string
that isn't parseable ranks below every one that is, so junk can never win
"latest".

Selection rules the client uses when resolving "which build":

- **Newest version**: `versions[0]` after the descending sort — the entry with
  the highest version, *not* the most recently published one.
- **A specific version string** (e.g. a user picking `1.0.0`): the client
  filters by the embedded `manifest.version`. If several entries share a
  version string (different `rootHash`), the newest by `releasedAt` wins.
- **An exact build**: `(version, rootHash)` identifies one entry uniquely.
  That pair is also the **dedup key** — a generator must never emit two
  entries with the same `(version, rootHash)` under one package (`index.py
  validate` flags it).

`releasedAt` is a *made-available* time, not a build time. In the
GitHub-Actions flow it's the `.lgx` asset's HTTP `Last-Modified`; for a
self-hosted catalog built with `index.py` it's the same, or the local file's
mtime, or the run time as a last resort. Minute-level differences between two
regenerations of the same catalog are expected and harmless — and, now that it
no longer drives the ordering, inconsequential.

---

## 7. The download verification contract

This is the security-relevant heart of the format. `index.json` is fetched
from one place and each `.lgx` from another (`url`), so nothing about the
transport alone proves the file you downloaded is the build the catalog
advertised. The client closes that gap: **after downloading a `.lgx` and
before installing it**, it verifies the file against the index entry
(`verifyDownloadAgainstIndex`). A mismatch aborts the install.

The checks, in order:

1. **Structural soundness.** `lgx verify` confirms the file is a
   well-formed `.lgx` whose internal Merkle hashes are self-consistent.
   Catches truncation/corruption and a non-`.lgx` served at the URL. This
   also makes the file's own `manifest.hashes.root` trustworthy as its real
   content root.
2. **`rootHash` binding.** The index entry's `rootHash` must equal the
   file's `manifest.hashes.root`. The strongest check — a content
   fingerprint. Skipped only if the entry omits `rootHash`.
3. **`manifest` binding.** The file's manifest must match the index entry's
   embedded `manifest` on the five binding fields: **`name`, `version`,
   `main`, `dependencies`, `type`**. (Cosmetic fields like `description`
   are *not* compared — an index builder may normalise them.)
4. **Signer binding.** If the index entry advertised `signature.did`, the
   downloaded file must be signed by the **same** DID. (Whether that DID is
   *trusted* — checked against `trustedSigners` — is a separate,
   client-policy step layered on top.)

A facet is skipped only when the index advertised nothing to check it
against (no `rootHash`, no `manifest`, no `signature`). A real mismatch is
always a hard failure.

> Note the asymmetry with `index.py validate --full`: that offline tool
> *additionally* checks `sha256` and `size` against the downloaded bytes,
> because at publish time it has both halves in hand. The install-time
> client deliberately binds on `rootHash` (content) rather than `sha256`
> (exact bytes), so a benignly-repackaged-but-content-identical `.lgx`
> still installs.

---

## 8. Fields the client synthesises at read time

When the client merges configured repos into its in-memory catalog
(`getCatalogJson`), it **adds** fields that are **not** part of the
on-disk `index.json` and must **not** be written into it:

| Synthesised field | Source |
|---|---|
| `repositoryUrl` | the `logos-repo.json` URL the index came from |
| `repositoryName` | the repo's `name` (per-package, not the index header) |
| `repositoryDisplayName` | the repo's `displayName` |
| `description`, `type`, `category`, `author`, `icon` (at the **package** level) | lifted from `versions[0].manifest` |
| `topLevel` (on dependency-resolution output) | added by the resolver to mark caller-requested vs. transitive packages |

If you see these in client output or API responses, they're runtime
additions — your `index.json` should contain only the fields documented in
§3.

---

## 9. Schema versioning and compatibility

- **`index.json` is `schemaVersion: 2`.** This is the current format and
  what `index.py` writes and validates. Version 1 was an earlier,
  GitHub-release-centric shape; clients no longer produce it.
- **`logos-repo.json` is `schemaVersion: 1`** by convention (advisory —
  §2.4).
- **Adding optional fields is backward-compatible.** Clients ignore unknown
  fields. You can enrich either file with extra keys without breaking older
  clients — but don't expect older clients to *act* on them.
- **A major bump signals a breaking change** to required fields or their
  meaning. Don't change the meaning of an existing field in place; add a
  new one and bump the version.
- **Be liberal in what you accept.** The reference client is null-safe and
  defensive: `manifest: null` rows from early generators don't crash it,
  unparseable repos are dropped (not fatal), and missing optional fields
  default to empty. Producers should still aim for the clean shape here —
  the leniency is a safety net, not a spec.

---

## 10. Producing and validating an index

You should never write `index.json` by hand. Use [`index.py`](../index.py)
(this repo) or the GitHub-Actions `rebuild-index.yml` (which calls the same
`index.py`). Both emit byte-compatible output.

```bash
# Build a fresh index from a list of .lgx URLs:
./index.py build urls.txt -o index.json --name my-catalog

# Incrementally add / remove without a full rebuild:
./index.py add    index.json https://example.com/foo-1.2.0.lgx
./index.py remove index.json foo 1.2.0

# Inspect (no network, no lgx):
./index.py list index.json
./index.py show index.json foo

# Validate — light is structural/consistency; --full downloads every
# package and re-checks the index↔file bindings of §7 (plus sha256/size):
./index.py validate index.json
./index.py validate index.json --full
```

`build`, `add`, and `validate --full` shell out to the `lgx` binary to
verify each package and extract its manifest + signature, so those need
`lgx` on `PATH`:

```bash
nix build github:logos-co/logos-package#lgx
```

`logos-repo.json`, by contrast, *is* hand-edited — copy the example in §2.1,
set `name` / `displayName` / `indexUrl` to your catalog, and add your
`trustedSigners`. See the [`logos-modules-release-base`](https://github.com/logos-co/logos-modules-release-base)
fork-me template for a full catalog skeleton.

---

## Quick reference

**`logos-repo.json`** — identity card, hand-edited, one per catalog:

```
name*          displayName*          indexUrl*           ← required
description    homepage              trustedSigners[]    ← optional
network
schemaVersion (advisory, =1)
trustedSigners[] = { did*, name? }
```

**`index.json`** — package listing, generated, `schemaVersion: 2`:

```
schemaVersion*  repositoryName*  generatedAt*  packages[]*
packages[]      = { name*, versions[]* }
versions[]      = { url*, rootHash*, releasedAt*, manifest*,   ← required
                    signature?,                                 ← signed only
                    urls?,                                      ← extra sources
                    publisherRef?, size?, sha256? }             ← informational
manifest        = { name*, version*, type*, main*, dependencies*, hashes*,
                    manifestVersion, category, author, description, icon, view }
signature       = { did*, algorithm, signature, signer, linkedDids, version }
```

`*` = required / binding. The client binds a download to its index entry on
`rootHash`, `manifest.{name,version,main,dependencies,type}`, and
`signature.did` (§7).
