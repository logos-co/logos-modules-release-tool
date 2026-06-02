# logos-modules-release-tool

`index.py` — a single command-line tool to build, edit, and inspect a Logos
module catalog's `index.json`. Works against `.lgx` files hosted **anywhere**:
GitHub Releases, S3, your own server, or just local files on disk. Output is
byte-compatible with what the GitHub-Actions `rebuild-index.yml` workflow in
[`logos-modules-release-action`](https://github.com/logos-co/logos-modules-release-action)
produces (in fact, that workflow uses this tool), so clients (`lgpd`, the
`package_downloader` module, the package-manager UI) consume it identically.

## Install

`index.py` is a single Python 3 file with no dependencies besides the
standard library. Clone the repo and run it directly:

```bash
git clone https://github.com/logos-co/logos-modules-release-tool
./logos-modules-release-tool/index.py --help
```

You also need the `lgx` binary for any subcommand that has to read a `.lgx`
file (`build`, `add`, `validate --full`):

```bash
nix build github:logos-co/logos-package#lgx
# Or grab a pre-built release: https://github.com/logos-co/logos-package/releases
```

`remove`, `list`, `show`, and `validate` (without `--full`) are pure JSON
operations — no `lgx` needed and no network traffic.

## Subcommands

```
index.py build    <urls-file>         [-o index.json] [--name NAME]
                                      [--with-local URL PATH ...] [--fetch {missing,all,none}]
index.py add      <index.json> [<url>...] [--from-file FILE]
                                      [--with-local URL PATH ...] [--fetch {missing,all,none}]
index.py remove   <index.json> <package> [version]
index.py list     <index.json>
index.py show     <index.json> <package>
index.py validate <index.json> [--full] [--pairs-file FILE]
                                      [--with-local URL PATH ...] [--fetch {missing,all,none}]
```

### `build` — full rebuild from a URL list

```bash
# urls-file: one entry per line — either `<url>` or `<url> <local-path>`
./index.py build urls.txt -o index.json --name "My Catalog"
```

`--name` falls back to the `name` field of `./logos-repo.json` if present,
then to `"unknown"`.

### `add` — merge new versions into an existing index

```bash
# Add by URL — the file is downloaded, verified, indexed.
./index.py add index.json https://my-host/foo-v1.0.0.lgx

# Add many at once from a file.
./index.py add index.json --from-file new-urls.txt

# The upload-then-index flow: you just built foo.lgx and uploaded it.
# Index the URL but read all metadata from the on-disk file — no
# re-download.
./index.py add index.json --with-local https://my-host/foo.lgx ./dist/foo.lgx
```

Versions are deduped on `(version, rootHash)`: re-adding an identical
entry is a silent no-op.

### `remove` — drop a package or one of its versions

```bash
./index.py remove index.json foo 1.2.0    # one version
./index.py remove index.json foo          # whole package
```

If you remove the last remaining version, the empty package entry is
dropped automatically (clients never need to see empty entries).

### `list`, `show`, `validate` — inspect / sanity-check

```bash
./index.py list index.json                # header + one line per package
./index.py show index.json foo            # versions + details for one
./index.py validate index.json            # structural / internal consistency
./index.py validate index.json --full     # also download every package
                                          # and cross-check index ↔ file
```

`validate --full` enforces the same index↔file bindings the client
(`verifyDownloadAgainstIndex` in `package_downloader_lib.cpp`) enforces at
install time — `rootHash`, manifest fields, signer DID, `sha256`, `size`.
Both modes report **every** problem found in a single run and exit
non-zero on any.

## Input file format

`urls-file`, `--from-file`, and `--pairs-file` all use the same shape:

```
# Comments start with `#`. Blank lines are ignored. Each non-comment
# line is either a single URL …
https://example.com/foo-v1.0.0.lgx

# … or a URL followed by a local path (any whitespace works).
https://example.com/bar-v1.0.0.lgx   /home/me/uploads/bar.lgx
https://example.com/baz-v1.0.0.lgx	./dist/baz.lgx
```

Inline trailing `# ...` comments are stripped too.

## Fetch modes

Apply to `build`, `add`, and `validate --full`:

| `--fetch` | Behaviour |
|---|---|
| `missing` *(default)* | Use the paired local file if one was supplied; otherwise download. |
| `all` | Always download. Local paths are noted but ignored. |
| `none` | Never download. Every URL must have a local pairing or it's an error. |

## How it fits together

Three repos cooperate:

- [`logos-modules-release-base`](https://github.com/logos-co/logos-modules-release-base) —
  fork-me template for running a Logos module catalog. Ships
  `scripts/catalog.sh` (drive the GitHub-Actions workflows from the
  terminal) and `scripts/add-module.sh`.
- [`logos-modules-release-action`](https://github.com/logos-co/logos-modules-release-action) —
  the build / sign / publish pipeline used by the catalog's workflows.
  Its `rebuild-index.yml` reusable workflow invokes **this** tool.
- **`logos-modules-release-tool`** *(you are here)* — the single
  `index.json` builder / editor / validator, used both by humans and by
  the action.

Catalog maintainers who want a fully non-GitHub setup (host `.lgx` files
on their own server, no Actions tab) can drive the entire publish
lifecycle from this tool alone. The GitHub Actions path stays available
for anyone who wants it.
