#!/usr/bin/env python3
"""Tests for index.py's catalog ordering.

Standard library only (`python3 -m unittest`), matching index.py's own
no-third-party-deps contract.

These need the `lgx` binary on PATH — ordering is deliberately delegated to
`lgx semver` so the catalog cannot disagree with the C++ clients about which
version is newest. Without lgx the ordering tests skip rather than silently
pass, which would defeat the point.

    nix build github:logos-co/logos-package#lgx   # then put ./result/bin on PATH
"""

import shutil
import subprocess
import unittest

import index


def lgx_has_semver() -> bool:
    if shutil.which("lgx") is None:
        return False
    r = subprocess.run(["lgx", "semver", "compare", "1.0.0", "1.0.0"],
                       capture_output=True)
    return r.returncode == 0


requires_lgx = unittest.skipUnless(
    lgx_has_semver(), "needs an `lgx` with the `semver` subcommand on PATH")


def entry(version: str, released_at: str, root_hash: str = "h") -> dict:
    """A minimal index version entry — only the fields ordering looks at."""
    return {
        "releasedAt": released_at,
        "rootHash": root_hash,
        "manifest": {"name": "demo_module", "version": version},
    }


def catalog(*entries: dict) -> dict:
    return {"packages": [{"name": "demo_module", "versions": list(entries)}]}


def ordered_versions(index_doc: dict) -> list:
    return [index.entry_version(v) for v in index_doc["packages"][0]["versions"]]


@requires_lgx
class TestSortVersions(unittest.TestCase):

    def test_orders_by_semver_not_by_release_date(self):
        """The bug this replaced.

        A pre-release cut *after* a stable release used to land at versions[0],
        and every client reads versions[0] as "latest" — so publishing
        2.0.0-alpha after 1.9.0 advertised the alpha to everyone.
        """
        doc = catalog(
            entry("2.0.0-alpha", "2026-06-01T00:00:00Z"),  # newest by DATE
            entry("1.9.0",       "2026-05-01T00:00:00Z"),  # newest by VERSION
        )
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc), ["2.0.0-alpha", "1.9.0"])

        # ...and the stable release must outrank a pre-release published later.
        doc = catalog(
            entry("2.0.0-alpha", "2026-06-01T00:00:00Z"),
            entry("2.0.0",       "2026-05-01T00:00:00Z"),
        )
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc)[0], "2.0.0",
                         "a stable release must outrank its own later-published alpha")

    def test_backport_published_later_does_not_become_latest(self):
        """A 1.2.1 hotfix cut after 2.0.0 shipped has a newer timestamp but is
        an older version. It used to take versions[0]."""
        doc = catalog(
            entry("1.2.1", "2026-06-01T00:00:00Z"),
            entry("2.0.0", "2026-01-01T00:00:00Z"),
        )
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc), ["2.0.0", "1.2.1"])

    def test_numeric_prerelease_identifiers_order_numerically(self):
        """Spec §11: rc.11 is newer than rc.2. A string sort says otherwise."""
        doc = catalog(
            entry("1.0.0-rc.2",  "2026-01-01T00:00:00Z"),
            entry("1.0.0-rc.11", "2026-01-02T00:00:00Z"),
            entry("1.0.0",       "2026-01-03T00:00:00Z"),
        )
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc),
                         ["1.0.0", "1.0.0-rc.11", "1.0.0-rc.2"])

    def test_ordering_is_independent_of_release_dates(self):
        """Same versions, dates deliberately inverted — the order must not move."""
        ascending_dates = catalog(
            entry("1.0.0", "2026-01-01T00:00:00Z"),
            entry("2.0.0", "2026-02-01T00:00:00Z"),
            entry("1.5.0", "2026-03-01T00:00:00Z"),
        )
        descending_dates = catalog(
            entry("1.0.0", "2026-03-01T00:00:00Z"),
            entry("2.0.0", "2026-02-01T00:00:00Z"),
            entry("1.5.0", "2026-01-01T00:00:00Z"),
        )
        index.sort_versions(ascending_dates)
        index.sort_versions(descending_dates)
        self.assertEqual(ordered_versions(ascending_dates), ["2.0.0", "1.5.0", "1.0.0"])
        self.assertEqual(ordered_versions(descending_dates), ["2.0.0", "1.5.0", "1.0.0"])

    def test_releasedAt_breaks_ties_within_one_version(self):
        """The same version republished (different rootHash): newest publish wins."""
        doc = catalog(
            entry("1.0.0", "2026-01-01T00:00:00Z", root_hash="old"),
            entry("1.0.0", "2026-02-01T00:00:00Z", root_hash="new"),
        )
        index.sort_versions(doc)
        hashes = [v["rootHash"] for v in doc["packages"][0]["versions"]]
        self.assertEqual(hashes, ["new", "old"])

    def test_unparseable_versions_sort_last(self):
        """A junk version string must never win "latest"."""
        doc = catalog(
            entry("banana", "2026-09-01T00:00:00Z"),
            entry("1.0.0",  "2026-01-01T00:00:00Z"),
        )
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc), ["1.0.0", "banana"])

    def test_single_and_empty_version_lists_are_untouched(self):
        doc = catalog(entry("1.0.0", "2026-01-01T00:00:00Z"))
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc), ["1.0.0"])


@requires_lgx
class TestValidateVersionOrder(unittest.TestCase):

    def test_flags_a_semver_misordered_catalog(self):
        issues = index.check_version_order(
            "packages[0]", "demo_module",
            [entry("1.0.0", "2026-02-01T00:00:00Z"),
             entry("2.0.0", "2026-01-01T00:00:00Z")])
        self.assertTrue(issues)
        self.assertIn("out of order", issues[0])

    def test_accepts_a_correctly_ordered_catalog(self):
        # Note the dates run "backwards" — that is legal and must not be flagged,
        # which the old descending-releasedAt check got wrong.
        issues = index.check_version_order(
            "packages[0]", "demo_module",
            [entry("2.0.0", "2026-01-01T00:00:00Z"),
             entry("1.0.0", "2026-02-01T00:00:00Z")])
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
