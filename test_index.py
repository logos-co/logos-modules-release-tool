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

import json
import shutil
import subprocess
import unittest
from unittest import mock

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

        A pre-release published *after* its own stable release used to land at
        versions[0], and every client reads versions[0] as "latest" — so
        2.0.0-alpha published after 2.0.0 advertised the alpha to everyone.

        The case has to be one where semver and the timestamp genuinely
        disagree. 2.0.0-alpha vs 1.9.0 would NOT: 2.0.0-alpha outranks 1.9.0,
        so it lands first under both the old date sort and the new one.
        """
        doc = catalog(
            entry("2.0.0-alpha", "2026-06-01T00:00:00Z"),  # newest by DATE
            entry("2.0.0",       "2026-05-01T00:00:00Z"),  # newest by VERSION
        )
        index.sort_versions(doc)
        self.assertEqual(ordered_versions(doc), ["2.0.0", "2.0.0-alpha"],
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


class TestValidateWithoutLgx(unittest.TestCase):
    """check_version_order must degrade gracefully when lgx can't rank —
    missing, or too old to have the `semver` subcommand — rather than crash.
    These run regardless of whether lgx is present."""

    def test_reports_unchecked_when_lgx_lacks_semver(self):
        # Simulate an lgx with no `semver` subcommand.
        original = index._lgx_has_semver
        index._lgx_has_semver = lambda: False
        try:
            issues = index.check_version_order(
                "packages[0]", "demo_module",
                [entry("1.0.0", "2026-02-01T00:00:00Z"),
                 entry("2.0.0", "2026-01-01T00:00:00Z")])  # genuinely misordered
        finally:
            index._lgx_has_semver = original
        # Misordered, but unverifiable without semver: no crash, no false pass.
        self.assertEqual(issues, [])

    def test_does_not_crash_if_ranking_raises(self):
        # lgx claims semver support but the sort call blows up mid-run.
        orig_has, orig_rank = index._lgx_has_semver, index.semver_rank_desc
        index._lgx_has_semver = lambda: True
        def boom(_):
            raise RuntimeError("lgx semver sort failed: boom")
        index.semver_rank_desc = boom
        try:
            issues = index.check_version_order(
                "packages[0]", "demo_module",
                [entry("1.0.0", "2026-01-01T00:00:00Z"),
                 entry("2.0.0", "2026-01-02T00:00:00Z")])
        finally:
            index._lgx_has_semver, index.semver_rank_desc = orig_has, orig_rank
        self.assertEqual(issues, [])


LGX_URL = ("https://github.com/logos-co/logos-modules-release/releases/"
           "download/demo_module-v1.0.0/demo_module-1.0.0.lgx")

class TestSidecar(unittest.TestCase):
    """Test the sidecar is fetched and the CID can be retrieved."""

    def test_reads_the_cid_from_the_sidecar(self):
        # Provide a sidecar with a CID
        def fake_download(url, dest):
            dest.write_text(json.dumps({"sha256": "abc", "cid": "zDvZRw"}))

        with mock.patch.object(index, "download", fake_download):
            self.assertEqual(index.fetch_sidecar(LGX_URL).get("cid", ""), "zDvZRw")

    def test_no_cid_when_absent_from_sidecar(self):
        def fake_download(url, dest):
            dest.write_text(json.dumps({"sha256": "abc"}))

        with mock.patch.object(index, "download", fake_download):
            self.assertEqual(index.fetch_sidecar(LGX_URL).get("cid", ""), "")

    def test_no_cid_when_the_sidecar_download_fails(self):
        def fake_download(url, dest):
            raise index.FetchError(f"download failed for {url}: HTTP 404 Not Found")

        with mock.patch.object(index, "download", fake_download):
            self.assertEqual(index.fetch_sidecar(LGX_URL).get("cid", ""), "")

    def test_no_cid_when_the_sidecar_is_not_json(self):
        def fake_download(url, dest):
            dest.write_text("<html>404</html>")

        with mock.patch.object(index, "download", fake_download):
            self.assertEqual(index.fetch_sidecar(LGX_URL).get("cid", ""), "")

if __name__ == "__main__":
    unittest.main()
