#!/usr/bin/env python3
# Local index.json manager for a self-hosted Logos module catalog.
#
# The GitHub-Actions path (rebuild-index.yml → logos-modules-release-action's
# scripts/build_index.py) builds index.json by walking GitHub release tags
# and reading a sidecar.json baked into each release. This script does the
# same job from a plain URL list, so a catalog can host its .lgx files
# anywhere (S3, your own server, …) and still produce / mutate / inspect a
# byte-compatible index.json. Clients (lgpd, the package_downloader module,
# the package-manager UI) consume the output unchanged.
#
# Per-version entry format mirrors build_index.py:
#   { releasedAt, publisherRef, url, size, sha256, rootHash,
#     manifest, signature? }
#
# Usage:
#   ./index.py build    <urls-file>         [-o index.json] [--name NAME]
#                                           [--with-local URL PATH ...] [--fetch {missing,all,none}]
#   ./index.py add      <index.json> [<url>...] [--from-file FILE]
#                                           [--with-local URL PATH ...] [--fetch {missing,all,none}]
#   ./index.py remove   <index.json> <package> [version]
#   ./index.py list     <index.json>
#   ./index.py show     <index.json> <package>
#   ./index.py validate <index.json> [--full] [--pairs-file FILE]
#                                           [--with-local URL PATH ...] [--fetch {missing,all,none}]
#
# Input file format (urls-file / --from-file / --pairs-file): one entry
# per line, either `<url>` or `<url> <local-path>` (whitespace-separated).
# Blank lines and `#` comments are ignored.
#
# Fetch modes (build / add / validate --full):
#   --fetch missing  default — use the paired local file if one was
#                    supplied, otherwise download
#   --fetch all      always download; paired locals are ignored
#   --fetch none     never download; an unpaired URL is an error
#                    (build/add abort, validate reports per-entry)
#
# `build`, `add`, and `validate --full` require the `lgx` binary on PATH
# (every package is verified). `remove` / `list` / `show` need no network.
#
# `lgx` also owns VERSION ORDERING. `versions[]` is stored newest-first by
# SemVer 2.0.0 precedence (see sort_versions), and that order is computed by
# `lgx semver` rather than reimplemented here — it is the same implementation
# the C++ clients (lgpm, lgpd, the package-manager UI) use, so the catalog
# cannot disagree with them about which version is newest. `validate` (light)
# therefore also consults `lgx` when it is present, and reports the ordering as
# unchecked when it isn't.
#
# Install lgx:   nix build github:logos-co/logos-package#lgx
#
# Standard library only — `urllib`, `tarfile`-free (manifest + signature
# come from `lgx`), no third-party deps.

import argparse
import email.utils
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

# How long to wait for a single socket read while downloading a .lgx
# before giving up. urllib.request.urlopen's `timeout=` applies per
# read(), so a stalled transfer mid-download is caught as well as
# unreachable hosts. 60s is generous enough for big packages over
# slow links but short enough that a dead URL doesn't hang the
# script forever.
DOWNLOAD_TIMEOUT_SECONDS = 60


class FetchError(Exception):
    """Raised by the download / verify / extract helpers on any
    package-level failure (download error, lgx verify failure,
    unparseable manifest, …). Wrapping these in an exception lets
    `cmd_build` / `cmd_add` translate to `die()` (single bad package
    aborts the run — the locked policy), while `validate --full`
    converts each one into a per-entry issue so the report covers the
    whole batch rather than stopping at the first miss.

    Previously these were direct `die()` calls and validate caught the
    resulting SystemExit only to immediately re-raise it — a bug PR
    review surfaced."""

SCHEMA_VERSION = 2
# Manifest fields the downloader cross-checks index ↔ file on
# (verifyDownloadAgainstIndex in package_downloader_lib.cpp). Keep this
# in sync with the C++ side — divergence would mean validate --full
# accepts indexes the client would later reject at install time.
MANIFEST_BINDING_FIELDS = ("name", "version", "main", "dependencies", "type")


# ── stdio helpers ────────────────────────────────────────────────────────

def info(msg: str) -> None:
    # `==>` prefix matches add-module.sh / catalog.sh conventions.
    print(f"==> {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "_NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ── lgx wrappers ─────────────────────────────────────────────────────────

def require_lgx() -> None:
    """Preflight: abort early when `lgx` isn't on PATH so we don't get a
    cryptic FileNotFoundError mid-download."""
    if shutil.which("lgx") is None:
        die(
            "the `lgx` binary is required but not on PATH.\n"
            "       Install with:  nix build github:logos-co/logos-package#lgx\n"
            "       Or skip the install path: this script's `remove` / `list` /\n"
            "       `show` / `validate` (light) subcommands work without lgx."
        )


def _lgx_has_semver() -> bool:
    """True if an `lgx` with the `semver` subcommand is on PATH."""
    if shutil.which("lgx") is None:
        return False
    r = subprocess.run(["lgx", "semver", "compare", "1.0.0", "1.0.0"],
                       capture_output=True)
    return r.returncode == 0


def require_lgx_semver() -> None:
    """Preflight: the `lgx` on PATH must have the `semver` subcommand.

    Catalog ordering is delegated to it (see semver_rank_desc). An older lgx
    predates it — abort loudly rather than silently falling back to sorting by
    release date, which is the bug this replaced and which is invisible in the
    output."""
    if not _lgx_has_semver():
        die(
            "the `lgx` on PATH has no `semver` subcommand (it predates it).\n"
            "       Version ordering is delegated to lgx so the catalog agrees\n"
            "       with the clients; refusing to fall back to a date sort.\n"
            "       Update with:  nix build github:logos-co/logos-package#lgx"
        )


def lgx_run(*args: str) -> bytes:
    """Run `lgx <args>` and return stdout bytes. Raises RuntimeError on
    non-zero exit, with the (decoded) stderr in the message."""
    r = subprocess.run(["lgx", *args], capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        raise RuntimeError(f"lgx {' '.join(args)} failed: {err}")
    return r.stdout


# ── time helpers ─────────────────────────────────────────────────────────

def iso_now() -> str:
    """ISO-8601 UTC second-precision with trailing Z — the exact shape
    build_index.py emits, so a manually-built index reads identically
    when diffed against the GitHub-Actions output."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_http_date(http_date: str | None) -> str | None:
    """Convert an HTTP `Last-Modified` header (RFC 7231 / RFC 2822 format)
    to the ISO `Z` shape used in index entries. Returns None when the
    header is absent or unparseable."""
    if not http_date:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # parsedate_to_datetime may return a naive datetime (assumed UTC) or
    # an aware one — normalise both to aware-UTC before formatting.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── index I/O ────────────────────────────────────────────────────────────

def load_index(path: pathlib.Path) -> dict:
    """Read and minimally type-check an index.json. Used by every
    subcommand that operates on an existing index — load failure is
    fatal, structural defects surface here so subcommands don't have to
    re-validate the top-level shape themselves."""
    if not path.exists():
        die(f"index file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"failed to read {path}: {exc}")
    if not isinstance(data, dict) or "packages" not in data:
        die(f"{path} doesn't look like an index.json (no top-level `packages`)")
    if not isinstance(data["packages"], list):
        die(f"{path}: `packages` must be a JSON array")
    return data


def save_index(path: pathlib.Path, index: dict) -> None:
    """Write back with the same shape build_index.py uses — 2-space
    indent, sort_keys=False so the natural top-level field order
    (schemaVersion, repositoryName, generatedAt, packages) stays
    readable, trailing newline."""
    path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def resolve_repo_name(cli_name: str | None) -> str:
    """`repositoryName` precedence: --name > `logos-repo.json` in cwd >
    "unknown". Mirrors what build_index.py reads from the catalog
    repo's logos-repo.json so the generated index header looks the
    same."""
    if cli_name:
        return cli_name
    repo_file = pathlib.Path("logos-repo.json")
    if repo_file.exists():
        try:
            name = json.loads(repo_file.read_text(encoding="utf-8")).get("name", "")
            if isinstance(name, str) and name:
                return name
        except (OSError, json.JSONDecodeError):
            pass
    return "unknown"


# ── URL list parsing ─────────────────────────────────────────────────────

def read_url_pairs(path: pathlib.Path) -> list[tuple[str, str | None]]:
    """One entry per line, either `<url>` or `<url> <local-path>`.
    Blank lines and `#` comments are ignored; an inline ` #...` trailing
    comment is stripped too — common shell-style file convention.

    Returns a list of (url, local_path) tuples in file order, with
    `local_path = None` for URL-only lines. Backwards-compatible with
    the historical single-URL-per-line files."""
    pairs: list[tuple[str, str | None]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        # Strip an inline comment if present, but only when preceded by
        # whitespace — URLs never contain `#` followed by whitespace and
        # this keeps a `#` that's actually part of the URL intact.
        if " #" in raw:
            raw = raw.split(" #", 1)[0]
        elif "\t#" in raw:
            raw = raw.split("\t#", 1)[0]
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # `.split(None, 1)` splits on the first run of whitespace,
        # keeping any further whitespace inside the path token intact
        # (rare, but URLs and paths are user-supplied so don't lose it).
        toks = line.split(None, 1)
        url = toks[0]
        local_path = toks[1].strip() if len(toks) > 1 else None
        pairs.append((url, local_path))
    return pairs


def merge_cli_pairs(
    file_pairs: list[tuple[str, str | None]],
    cli_with_local: list[list[str]] | None,
) -> list[tuple[str, str | None]]:
    """Apply `--with-local URL PATH` overrides to a list of file pairs,
    preserving file order. A CLI pair whose URL is already present
    UPDATES the entry's local path; a CLI pair with a new URL is
    APPENDED at the end. Useful so `--with-local` works both for
    overriding what a file said and for adding pairs from the command
    line alone."""
    if not cli_with_local:
        return list(file_pairs)
    cli_map = {url: path for url, path in cli_with_local}
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for url, p in file_pairs:
        out.append((url, cli_map.get(url, p)))
        seen.add(url)
    for url, p in cli_with_local:
        if url not in seen:
            out.append((url, p))
            seen.add(url)
    return out


def iso_from_mtime(path: pathlib.Path) -> str | None:
    """Format a file's mtime as ISO-8601 UTC `Z`, matching how `iso_now`
    and `iso_from_http_date` shape their output. Used as the `releasedAt`
    source for the local-files mode of build/add — the closest "made-
    available" timestamp we have when the file never went through HTTP.
    Returns None on stat() failure so the caller can fall back to now."""
    try:
        mt = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mt, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── per-URL extraction (shared by build + add) ───────────────────────────

def download(url: str, dest: pathlib.Path) -> str | None:
    """Fetch a URL into `dest`, returning the response's Last-Modified
    header verbatim (or None when the server didn't send one). We don't
    parse it here — caller decides how to convert / fall back.

    Network/HTTP failures are turned into `FetchError` with a one-line
    message; the timeout applies per socket read so a stalled
    connection or unreachable host fails out in bounded time instead
    of hanging forever. Without this guard a transient failure
    surfaced as a raw Python traceback to the operator, which review
    correctly called out as unhelpful from a terminal."""
    info(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "logos-index.py/1"})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            last_modified = resp.headers.get("Last-Modified")
            with dest.open("wb") as f:
                shutil.copyfileobj(resp, f)
    except urllib.error.HTTPError as exc:
        # Server responded with an error status (404, 403, 500, …).
        raise FetchError(f"download failed for {url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        # Connection-level failure: DNS, refused, unreachable, TLS, …
        # `exc.reason` carries the underlying OSError / message.
        raise FetchError(f"download failed for {url}: {exc.reason}") from exc
    except socket.timeout as exc:
        # Stalled mid-transfer — urllib raises socket.timeout for
        # per-read deadlines on Python ≥ 3.10.
        raise FetchError(
            f"download timed out for {url} "
            f"(no data within {DOWNLOAD_TIMEOUT_SECONDS}s)"
        ) from exc
    except OSError as exc:
        # Catch-all for anything urllib lets through (e.g. disk full
        # mid-write). Keep the message single-line for terminal output.
        raise FetchError(f"download failed for {url}: {exc}") from exc
    return last_modified


def version_entry_from_lgx(
    url: str, lgx_path: pathlib.Path, released_at: str | None
) -> tuple[str, dict]:
    """Turn a .lgx file into (package_name, version_entry).

    `released_at` is an ISO-8601 UTC `Z` string already (HTTP
    Last-Modified converted upstream, or local file mtime, or None
    when neither is known — caller has already normalised the source
    of "made-available" time). When None, the entry's `releasedAt`
    falls back to `iso_now()`.

    Pipeline:
      1. `lgx verify` — raises FetchError on failure (locked policy:
         build/add abort on any bad package; validate --full reports
         it as an issue and moves on).
      2. `lgx manifest --json` for the full manifest (matches what
         build_index.py records).
      3. `lgx signature` for the raw manifest.sig (empty = unsigned).
      4. sha256 + size from the file bytes.

    The entry shape is byte-compatible with what build_index.py emits,
    so a diff against the GitHub-Actions index for the same packages
    differs only in `releasedAt` (Last-Modified / mtime vs the GH
    release time) and the top-level `generatedAt`.

    Every failure path raises FetchError rather than calling die() so
    the validate --full loop can convert per-entry failures into report
    issues without aborting the batch (PR review fix)."""
    info(f"verifying {lgx_path.name}")
    try:
        lgx_run("verify", str(lgx_path))
    except RuntimeError as exc:
        raise FetchError(f"package failed `lgx verify` ({url}): {exc}") from exc

    try:
        raw_manifest = lgx_run("manifest", str(lgx_path), "--json")
    except RuntimeError as exc:
        raise FetchError(f"{url}: `lgx manifest --json` failed: {exc}") from exc
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url}: package's manifest.json is unparseable: {exc}") from exc

    try:
        raw_sig = lgx_run("signature", str(lgx_path))
    except RuntimeError as exc:
        raise FetchError(f"{url}: `lgx signature` failed: {exc}") from exc
    signature: dict | None = None
    if raw_sig.strip():
        try:
            signature = json.loads(raw_sig)
        except json.JSONDecodeError as exc:
            raise FetchError(f"{url}: package's manifest.sig is unparseable: {exc}") from exc

    data = lgx_path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    size = len(data)

    name = manifest.get("name", "")
    version = manifest.get("version", "")
    if not name or not version:
        raise FetchError(f"{url}: manifest is missing `name` or `version`")
    root_hash = manifest.get("hashes", {}).get("root", "")
    if not root_hash:
        raise FetchError(f"{url}: manifest has no `hashes.root`")

    entry: dict = {
        "releasedAt": released_at or iso_now(),
        # `publisherRef` is informational (the client ignores it). In
        # the GH flow it's the release tag; for a self-hosted catalog
        # there isn't one, so we synthesise the same shape.
        "publisherRef": f"{name}-v{version}",
        "url": url,
        "size": size,
        "sha256": sha256,
        "rootHash": root_hash,
        "manifest": manifest,
    }
    if signature is not None:
        entry["signature"] = signature
    return name, entry


FETCH_MODES = ("missing", "all", "none")


def resolve_lgx(
    url: str,
    local_path: str | None,
    fetch_mode: str,
    workdir: pathlib.Path,
) -> tuple[pathlib.Path, str | None]:
    """Decide where the .lgx bytes come from for one (url, local_path)
    input, given the fetch mode. Returns (lgx_path, released_at_iso).

    The truth table the plan locked in:

      fetch_mode | local_path | action
      ---------- | ---------- | ---------------------------------------
      missing    | yes        | use local; releasedAt = file mtime
      missing    | no         | download
      all        | yes        | download (local ignored — info note)
      all        | no         | download
      none       | yes        | use local; releasedAt = file mtime
      none       | no         | raise FetchError (no network allowed)

    All errors raise FetchError so build/add can die() per the locked
    "single bad input aborts" policy and validate --full can collect
    issues per the batch-reporting contract."""
    if fetch_mode not in FETCH_MODES:
        raise FetchError(f"internal: unknown fetch mode {fetch_mode!r}")

    use_local = local_path is not None and fetch_mode != "all"
    if local_path is not None and fetch_mode == "all":
        # Surface the choice so a user mixing pairs + `--fetch all`
        # doesn't quietly wonder why their on-disk files weren't used.
        info(f"--fetch all: ignoring local path for {url}")

    if use_local:
        # mypy/runtime: use_local implies local_path is not None.
        assert local_path is not None
        p = pathlib.Path(local_path)
        if not p.exists():
            raise FetchError(
                f"local path not found for {url}: {local_path}"
            )
        if not p.is_file():
            raise FetchError(
                f"local path for {url} is not a regular file: {local_path}"
            )
        return p, iso_from_mtime(p)

    if fetch_mode == "none":
        raise FetchError(
            f"--fetch none: no local path supplied for {url} "
            f"(pair it via `<url> <path>` in the input file or "
            f"`--with-local {url} <path>` on the command line)"
        )

    # Disambiguate filenames so two URLs ending in the same basename
    # don't collide in workdir. The lgx file itself is opaque; the name
    # we pick is just a working handle.
    lgx_path = workdir / f"pkg-{len(list(workdir.iterdir())):04d}.lgx"
    last_modified = download(url, lgx_path)
    return lgx_path, iso_from_http_date(last_modified)


def fetch_entry(
    url: str,
    local_path: str | None,
    fetch_mode: str,
    workdir: pathlib.Path,
) -> tuple[str, dict]:
    """Resolve the .lgx (download or take from disk) and convert it to
    a (name, version_entry) tuple. The fetch decision lives in
    `resolve_lgx`; this wrapper exists so the build/add/validate loops
    can keep their per-URL shape and the downstream
    `version_entry_from_lgx` stays one code path regardless of where
    the bytes came from."""
    lgx_path, released_at = resolve_lgx(url, local_path, fetch_mode, workdir)
    return version_entry_from_lgx(url, lgx_path, released_at)


# ── index mutation helpers ───────────────────────────────────────────────

def find_package(index: dict, name: str) -> dict | None:
    """Linear scan by name. Catalogs are small (tens of entries); a hash
    index isn't worth the complexity."""
    for pkg in index["packages"]:
        if pkg.get("name") == name:
            return pkg
    return None


def merge_version(index: dict, name: str, entry: dict) -> bool:
    """Insert `entry` under the package called `name`, creating the
    package entry if it didn't exist. Dedupe on (version, rootHash) —
    an incoming exact duplicate is skipped (returns False). Caller
    re-sorts.

    Returns True when the index was modified."""
    incoming_v = entry["manifest"]["version"]
    incoming_h = entry["rootHash"]

    pkg = find_package(index, name)
    if pkg is None:
        index["packages"].append({"name": name, "versions": [entry]})
        return True

    for v in pkg["versions"]:
        if v.get("manifest", {}).get("version") == incoming_v \
                and v.get("rootHash") == incoming_h:
            info(f"skipping {name} v{incoming_v} — already present "
                 f"(rootHash matches)")
            return False
    pkg["versions"].append(entry)
    return True


def entry_version(entry: dict) -> str:
    """The version string of an index entry. `manifest` is legally null in
    catalogs produced by early action runs, hence the `or {}`."""
    manifest = entry.get("manifest") or {}
    return manifest.get("version") or ""


def semver_rank_desc(versions: list[str]) -> dict[str, int]:
    """Map each version to its rank, 0 = newest, by SemVer 2.0.0 precedence.

    Shells out to `lgx semver` rather than reimplementing semver here. `lgx`
    owns the single implementation that the C++ clients (lgpm, lgpd, the
    package-manager UI) also use, so the catalog cannot disagree with them
    about which version is newest — which is exactly how the ordering drifted
    before. It costs nothing: this script is stdlib-only but already requires
    `lgx` on PATH for the only two subcommands that sort (`build` / `add`).

    Entries with no version string rank last.
    """
    real = [v for v in versions if v]
    ordered: list[str] = []
    if real:
        out = lgx_run("semver", "sort", "--desc", *real).decode("utf-8", "replace")
        ordered = [line.strip() for line in out.splitlines() if line.strip()]
        if sorted(ordered) != sorted(real):
            raise RuntimeError(
                "lgx semver sort returned an unexpected set of versions "
                f"(asked for {sorted(real)}, got {sorted(ordered)})"
            )

    rank = {v: i for i, v in enumerate(ordered)}
    for v in versions:
        if not v:
            rank[v] = len(ordered)
    return rank


def sort_versions(index: dict) -> None:
    """Order each package's `versions` newest-first by SemVer precedence,
    tie-breaking on `releasedAt`.

    This used to sort on `releasedAt` alone. A release timestamp is not the
    same thing as a version, and every client — the downloader's resolver, the
    package-manager UI's row builder, `index.py list` — treats `versions[0]` as
    "latest". Publishing `1.2.1` (a backport) after `2.0.0` put the *lower*
    version at `versions[0]`; so did publishing `2.0.0-alpha` after `2.0.0`,
    since the pre-release ranks below its own release but carried the newer
    timestamp. A forced republish had the same effect (it refreshes the asset's
    Last-Modified, which is what `releasedAt` actually records). Note it is only
    a divergence when semver and the timestamp disagree: `2.0.0-alpha` published
    after `1.9.0` lands first under *both* orderings, because `2.0.0-alpha`
    genuinely outranks `1.9.0`.

    `releasedAt` still breaks ties *within* one version, e.g. the same version
    republished with a different rootHash.
    """
    for pkg in index["packages"]:
        entries = pkg["versions"]
        if len(entries) < 2:
            continue
        # Pre-sort by date; the rank sort below is stable, so this survives as
        # the tiebreak between entries sharing a version.
        entries.sort(key=lambda v: v.get("releasedAt", ""), reverse=True)
        rank = semver_rank_desc([entry_version(v) for v in entries])
        entries.sort(key=lambda v: rank[entry_version(v)])


def bump_generated_at(index: dict) -> None:
    index["generatedAt"] = iso_now()


# ── subcommand: build ────────────────────────────────────────────────────

def cmd_build(args: argparse.Namespace) -> int:
    require_lgx()
    require_lgx_semver()
    urls_file = pathlib.Path(args.urls_file)
    if not urls_file.exists():
        die(f"urls file not found: {urls_file}")
    pairs = merge_cli_pairs(read_url_pairs(urls_file), args.with_local)
    if not pairs:
        die(f"{urls_file}: no URLs found (blank lines and `#` comments are ignored)")

    index: dict = {
        "schemaVersion": SCHEMA_VERSION,
        "repositoryName": resolve_repo_name(args.name),
        "generatedAt": iso_now(),
        "packages": [],
    }

    with tempfile.TemporaryDirectory(prefix="logos-index-") as tmpdir:
        workdir = pathlib.Path(tmpdir)
        for url, local_path in pairs:
            try:
                name, entry = fetch_entry(url, local_path, args.fetch, workdir)
            except FetchError as exc:
                # Single-package failure aborts the build (locked
                # policy: a bad input is not silently dropped).
                die(str(exc))
            merge_version(index, name, entry)

    sort_versions(index)
    out = pathlib.Path(args.output)
    save_index(out, index)
    info(f"wrote {out} — {len(index['packages'])} package(s)")
    return 0


# ── subcommand: add ──────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> int:
    require_lgx()
    require_lgx_semver()
    index_path = pathlib.Path(args.index)
    index = load_index(index_path)

    # Build the (url, local_path) input list in precedence order:
    # positional URLs first (URL-only, no pair), then any --from-file
    # entries (which may be paired). Then --with-local CLI overrides
    # apply on top (updating known URLs or appending new ones).
    pairs: list[tuple[str, str | None]] = [(u, None) for u in (args.url or [])]
    if args.from_file:
        pairs.extend(read_url_pairs(pathlib.Path(args.from_file)))
    pairs = merge_cli_pairs(pairs, args.with_local)
    if not pairs:
        die("add: need at least one URL "
            "(positional, --from-file, or --with-local)")

    added = 0
    with tempfile.TemporaryDirectory(prefix="logos-index-") as tmpdir:
        workdir = pathlib.Path(tmpdir)
        for url, local_path in pairs:
            try:
                name, entry = fetch_entry(url, local_path, args.fetch, workdir)
            except FetchError as exc:
                # Same single-package-aborts policy as `build`.
                die(str(exc))
            if merge_version(index, name, entry):
                added += 1

    sort_versions(index)
    bump_generated_at(index)
    save_index(index_path, index)
    info(f"updated {index_path} — added {added} version(s) "
         f"({len(pairs) - added} skipped)")
    return 0


# ── subcommand: remove ───────────────────────────────────────────────────

def cmd_remove(args: argparse.Namespace) -> int:
    index_path = pathlib.Path(args.index)
    index = load_index(index_path)

    pkg = find_package(index, args.package)
    if pkg is None:
        die(f"package not found in index: {args.package}")

    if args.version is None:
        # Drop the whole package entry.
        n = len(pkg["versions"])
        index["packages"] = [
            p for p in index["packages"] if p.get("name") != args.package
        ]
        info(f"removed package `{args.package}` ({n} version(s))")
    else:
        # Drop every entry matching the version string (multiple are
        # technically possible if different rootHashes share a version
        # string — rare, but the index allows it; remove them all so the
        # command's contract is "this version is gone").
        before = len(pkg["versions"])
        pkg["versions"] = [
            v for v in pkg["versions"]
            if v.get("manifest", {}).get("version") != args.version
        ]
        removed = before - len(pkg["versions"])
        if removed == 0:
            die(f"version not found: {args.package} v{args.version}")
        # Empty package entry is useless to clients; drop it.
        if not pkg["versions"]:
            index["packages"] = [
                p for p in index["packages"] if p.get("name") != args.package
            ]
            info(f"removed `{args.package}` v{args.version} "
                 f"(was the last version — package dropped)")
        else:
            info(f"removed `{args.package}` v{args.version} "
                 f"({removed} entry(ies))")

    bump_generated_at(index)
    save_index(index_path, index)
    return 0


# ── subcommand: list ─────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> int:
    index = load_index(pathlib.Path(args.index))
    print(f"repositoryName: {index.get('repositoryName', '?')}")
    print(f"schemaVersion:  {index.get('schemaVersion', '?')}")
    print(f"generatedAt:    {index.get('generatedAt', '?')}")
    print(f"packages:       {len(index['packages'])}")
    if not index["packages"]:
        return 0
    print()
    # Column-align: name | versions | latest
    name_w = max(len(p.get("name", "")) for p in index["packages"])
    name_w = max(name_w, len("name"))
    print(f"{'name':<{name_w}}  versions  latest")
    print(f"{'-' * name_w}  --------  ------")
    for pkg in index["packages"]:
        name = pkg.get("name", "?")
        versions = pkg.get("versions", [])
        latest = versions[0].get("manifest", {}).get("version", "?") if versions else "(none)"
        print(f"{name:<{name_w}}  {len(versions):>8}  {latest}")
    return 0


# ── subcommand: show ─────────────────────────────────────────────────────

def cmd_show(args: argparse.Namespace) -> int:
    index = load_index(pathlib.Path(args.index))
    pkg = find_package(index, args.package)
    if pkg is None:
        die(f"package not found in index: {args.package}")

    print(f"package: {pkg.get('name')}")
    print(f"versions ({len(pkg.get('versions', []))}):")
    for v in pkg.get("versions", []):
        version = v.get("manifest", {}).get("version", "?")
        released = v.get("releasedAt", "?")
        root_hash = v.get("rootHash", "")
        short_hash = root_hash[:12] + "…" if len(root_hash) > 12 else root_hash
        sig = v.get("signature") or {}
        signer = sig.get("did", "") if isinstance(sig, dict) else ""
        signed_marker = signer if signer else "(unsigned)"
        url = v.get("url", "")
        print(f"  v{version}")
        print(f"    releasedAt: {released}")
        print(f"    rootHash:   {short_hash}")
        print(f"    signed:     {signed_marker}")
        print(f"    url:        {url}")
    return 0


# ── subcommand: validate ─────────────────────────────────────────────────

def check_version_order(ctx: str, name: str, versions: list) -> list[str]:
    """`versions` must be newest-first by SemVer precedence, since every client
    reads `versions[0]` as "latest".

    This used to assert descending `releasedAt`. That check now has to go, and
    not just because the sort changed: it would actively *reject* a correctly
    ordered catalog, because a higher version legitimately carries an older
    timestamp (a backport, or a forced republish refreshing Last-Modified).

    Deciding the order needs semver, and semver lives in `lgx`. Light validate
    is documented as working without `lgx`, so when `lgx` is missing — or is too
    old to have the `semver` subcommand — we report the ordering as unchecked
    rather than crashing or quietly passing it.
    """
    if not _lgx_has_semver():
        warn(f"{ctx} ({name}): version ordering not checked — "
             "`lgx` is missing or has no `semver` subcommand")
        return []

    actual = [entry_version(v) for v in versions if isinstance(v, dict)]
    if len(actual) < 2:
        return []

    try:
        rank = semver_rank_desc(actual)
    except RuntimeError as e:
        warn(f"{ctx} ({name}): version ordering not checked — {e}")
        return []
    for i in range(len(actual) - 1):
        if rank[actual[i]] > rank[actual[i + 1]]:
            return [
                f"{ctx} ({name}): out of order — versions must be sorted newest-first "
                f"by semver precedence, but {actual[i]!r} precedes {actual[i + 1]!r}"
            ]
    return []


def _validate_light(index_path: pathlib.Path, index: dict) -> list[str]:
    """Structural + internal consistency. No network; uses `lgx` only to check
    version ordering, and says so when it can't (see check_version_order).

    Returns a list of human-readable problems (empty = clean)."""
    issues: list[str] = []
    # Header
    if index.get("schemaVersion") != SCHEMA_VERSION:
        issues.append(
            f"schemaVersion: expected {SCHEMA_VERSION}, "
            f"got {index.get('schemaVersion')!r}"
        )
    if not isinstance(index.get("repositoryName"), str) or not index["repositoryName"]:
        issues.append("repositoryName: missing or not a non-empty string")
    if not isinstance(index.get("generatedAt"), str):
        issues.append("generatedAt: missing or not a string")

    # Packages
    pkg_names_seen: set[str] = set()
    for pi, pkg in enumerate(index["packages"]):
        ctx = f"packages[{pi}]"
        if not isinstance(pkg, dict):
            issues.append(f"{ctx}: not an object")
            continue
        name = pkg.get("name")
        if not isinstance(name, str) or not name:
            issues.append(f"{ctx}: `name` missing or not a non-empty string")
            continue
        if name in pkg_names_seen:
            issues.append(f"{ctx}: duplicate package name {name!r}")
        pkg_names_seen.add(name)

        versions = pkg.get("versions")
        if not isinstance(versions, list) or not versions:
            issues.append(f"{ctx} ({name}): `versions` must be a non-empty array")
            continue

        seen_keys: set[tuple[str, str]] = set()
        # Ordering is checked once per package, after the per-entry walk — it is
        # a property of the sequence, not of any single entry, and it needs
        # `lgx semver` (see check_version_order).
        issues.extend(check_version_order(ctx, name, versions))
        for vi, v in enumerate(versions):
            vctx = f"{ctx}.versions[{vi}] ({name})"
            if not isinstance(v, dict):
                issues.append(f"{vctx}: not an object")
                continue
            # Required fields the downloader (and findBest) lean on.
            for required in ("releasedAt", "url", "rootHash", "manifest"):
                if required not in v:
                    issues.append(f"{vctx}: missing `{required}`")
            manifest = v.get("manifest")
            if not isinstance(manifest, dict):
                issues.append(f"{vctx}: `manifest` must be an object")
                continue
            for required in MANIFEST_BINDING_FIELDS:
                if required not in manifest:
                    issues.append(f"{vctx}: manifest missing `{required}`")
            mname = manifest.get("name")
            if mname is not None and mname != name:
                issues.append(
                    f"{vctx}: manifest.name {mname!r} != package name {name!r}"
                )
            # Dedup key
            key = (manifest.get("version", ""), v.get("rootHash", ""))
            if key in seen_keys:
                issues.append(
                    f"{vctx}: duplicate (version, rootHash)={key!r} within package"
                )
            seen_keys.add(key)
    return issues


def _validate_entry_against_file(
    pkg_name: str,
    entry: dict,
    local_path: str | None,
    fetch_mode: str,
    workdir: pathlib.Path,
) -> list[str]:
    """Cross-check one index entry against its actual .lgx file. Used by
    `--full` only. Mirrors the bindings the client enforces at install
    time in verifyDownloadAgainstIndex (rootHash + manifest fields +
    signer DID) — running this offline catches the same mismatches the
    download path would, but at index-publish time.

    `local_path` (when not None) skips the download; with `fetch_mode
    none` an unpaired entry surfaces as an issue rather than aborting
    the whole batch."""
    issues: list[str] = []
    url = entry.get("url", "")
    if not url:
        issues.append(f"{pkg_name}: entry has no url; cannot full-validate")
        return issues
    try:
        # Reuse the same fetch+verify+extract pipeline `add`/`build`
        # use. Per-entry FetchError becomes an issue and the caller
        # keeps going — so validate's contract ("report the whole
        # batch") is honoured even when several packages are broken.
        # Previously fetch_entry called die() and this function caught
        # the resulting SystemExit only to re-raise it, defeating the
        # batch contract; PR review caught the bug and the entire
        # error path is now exception-based.
        observed_name, observed = fetch_entry(
            url, local_path, fetch_mode, workdir
        )
    except FetchError as exc:
        issues.append(f"{pkg_name}: {exc}")
        return issues

    # Cross-checks — mirror verifyDownloadAgainstIndex's bindings.
    if observed_name != pkg_name:
        issues.append(
            f"{pkg_name}: file's manifest.name {observed_name!r} doesn't match "
            f"index package name"
        )
    if entry.get("rootHash") != observed.get("rootHash"):
        issues.append(
            f"{pkg_name} v{entry.get('manifest', {}).get('version', '?')}: "
            f"rootHash mismatch (index={entry.get('rootHash')}, "
            f"file={observed.get('rootHash')})"
        )
    idx_manifest = entry.get("manifest", {}) or {}
    obs_manifest = observed.get("manifest", {}) or {}
    for field in MANIFEST_BINDING_FIELDS:
        a = idx_manifest.get(field)
        b = obs_manifest.get(field)
        if a != b:
            issues.append(
                f"{pkg_name} v{idx_manifest.get('version', '?')}: "
                f"manifest.{field} mismatch (index vs file)"
            )
    idx_sig = entry.get("signature") or {}
    obs_sig = observed.get("signature") or {}
    if isinstance(idx_sig, dict) and idx_sig.get("did"):
        if not isinstance(obs_sig, dict) or obs_sig.get("did") != idx_sig.get("did"):
            issues.append(
                f"{pkg_name} v{idx_manifest.get('version', '?')}: "
                f"signature.did mismatch (index={idx_sig.get('did')}, "
                f"file={obs_sig.get('did') if isinstance(obs_sig, dict) else None})"
            )
    if "sha256" in entry and entry["sha256"] != observed.get("sha256"):
        issues.append(
            f"{pkg_name} v{idx_manifest.get('version', '?')}: sha256 mismatch"
        )
    if "size" in entry and entry["size"] != observed.get("size"):
        issues.append(
            f"{pkg_name} v{idx_manifest.get('version', '?')}: size mismatch "
            f"({entry['size']} vs {observed.get('size')})"
        )
    return issues


def cmd_validate(args: argparse.Namespace) -> int:
    index_path = pathlib.Path(args.index)
    index = load_index(index_path)

    issues = _validate_light(index_path, index)

    if args.full:
        require_lgx()
        # Build a lookup `{url: local_path}` from --pairs-file +
        # --with-local. Pairs whose URL doesn't match an entry in the
        # index get a soft warning at the end (stale path the user
        # supplied, or a typo) but don't fail the run — we still want
        # the cross-check to cover the rest of the index.
        pair_pairs: list[tuple[str, str | None]] = []
        if args.pairs_file:
            pair_pairs.extend(read_url_pairs(pathlib.Path(args.pairs_file)))
        pair_pairs = merge_cli_pairs(pair_pairs, args.with_local)
        pair_lookup: dict[str, str | None] = {url: path for url, path in pair_pairs}
        index_urls: set[str] = set()

        info("light pass complete; running --full cross-check against URLs…")
        with tempfile.TemporaryDirectory(prefix="logos-index-validate-") as tmpdir:
            workdir = pathlib.Path(tmpdir)
            for pkg in index["packages"]:
                pkg_name = pkg.get("name", "?")
                for entry in pkg.get("versions", []):
                    entry_url = entry.get("url", "")
                    if entry_url:
                        index_urls.add(entry_url)
                    local_path = pair_lookup.get(entry_url)
                    issues.extend(
                        _validate_entry_against_file(
                            pkg_name, entry, local_path, args.fetch, workdir
                        )
                    )

        # Warn about pairs whose URL didn't match any index entry —
        # most often a stale path the user kept around or a typo. Soft
        # warning rather than an issue: the index itself is still fine.
        unused = [u for u in pair_lookup if u not in index_urls]
        for u in unused:
            warn(f"--with-local / --pairs-file URL not in index: {u}")

    if not issues:
        print(f"{index_path}: OK ({len(index['packages'])} package(s)"
              + (", full check)" if args.full else ", light check)"))
        return 0
    for problem in issues:
        print(problem)
    print(f"\n{len(issues)} issue(s) — index failed validation",
          file=sys.stderr)
    return 1


# ── argparse wiring ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="index.py",
        description="Manage a self-hosted Logos catalog's index.json. "
                    "See module-level docstring for the full subcommand reference.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    # ── Shared flags for URL-consuming subcommands ──
    # Defined inline because argparse subparsers don't share groups
    # cleanly (parents= works but obscures per-command help text). The
    # three flags below appear on build/add/validate identically.
    def _add_pair_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--with-local", nargs=2, action="append",
            metavar=("URL", "PATH"),
            help="declare one URL ↔ local-path pair on the command line "
                 "(repeatable). Overrides a same-URL entry from any file "
                 "input, or appends if the URL is new.",
        )
        p.add_argument(
            "--fetch", choices=FETCH_MODES, default="missing",
            help="missing (default): use the paired local file if one was "
                 "supplied, otherwise download. all: download every URL, "
                 "ignoring local paths. none: never download — every URL "
                 "must have a paired local.",
        )

    pb = sub.add_parser("build", help="full rebuild from a URL-list file")
    pb.add_argument("urls_file",
                    help="text file with one entry per line: either `<url>` "
                         "or `<url> <local-path>` (whitespace-separated). "
                         "Blank lines + `#` comments ignored.")
    pb.add_argument("-o", "--output", default="index.json",
                    help="output path (default: index.json)")
    pb.add_argument("--name", default=None,
                    help="repositoryName for the header (default: read from "
                         "./logos-repo.json, fallback to 'unknown')")
    _add_pair_flags(pb)
    pb.set_defaults(func=cmd_build)

    pa = sub.add_parser("add", help="merge new .lgx URLs into an existing index")
    pa.add_argument("index", help="index.json path (modified in place)")
    pa.add_argument("url", nargs="*",
                    help=".lgx URL(s) — positional (URL-only; for pairs, use "
                         "--from-file or --with-local)")
    pa.add_argument("--from-file", default=None,
                    help="read additional URLs from this file (lines may be "
                         "`<url>` or `<url> <local-path>`)")
    _add_pair_flags(pa)
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("remove", help="drop a package, or one of its versions")
    pr.add_argument("index", help="index.json path (modified in place)")
    pr.add_argument("package", help="package name to remove")
    pr.add_argument("version", nargs="?", default=None,
                    help="optional version string; omit to drop the whole package")
    pr.set_defaults(func=cmd_remove)

    pl = sub.add_parser("list", help="header + one line per package")
    pl.add_argument("index", help="index.json path")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="versions + details for one package")
    ps.add_argument("index", help="index.json path")
    ps.add_argument("package", help="package name")
    ps.set_defaults(func=cmd_show)

    pv = sub.add_parser("validate",
                        help="structural check (light); add --full to also "
                             "download every package and cross-check index↔file")
    pv.add_argument("index", help="index.json path")
    pv.add_argument("--full", action="store_true",
                    help="download (or read local) every package and verify "
                         "the index entry matches the actual .lgx — same "
                         "bindings the client enforces at install time")
    pv.add_argument("--pairs-file", default=None,
                    help="text file pairing index entry URLs with local "
                         "paths (`<url> <local-path>` per line). The URLs "
                         "must match index entry `url` fields exactly.")
    _add_pair_flags(pv)
    pv.set_defaults(func=cmd_validate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
