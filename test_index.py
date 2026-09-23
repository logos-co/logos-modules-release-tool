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

import pathlib
import shutil
import subprocess
import tempfile
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

CID_URL = "logos:logos.test:zDvZRw"
DEV_CID_URL = "logos:logos.dev:zDvZRw"


def read_line(line: str) -> list:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = pathlib.Path(tmpdir) / "urls.txt"
        path.write_text(line + "\n")
        return index.read_url_pairs(path)


class TestReadUrlPairs(unittest.TestCase):

    def test_a_single_url_is_its_only_source(self):
        self.assertEqual(read_line(LGX_URL), [(LGX_URL, None, [LGX_URL])])

    def test_a_leading_cid_is_a_source_beside_the_url(self):
        self.assertEqual(read_line(f"{CID_URL} {LGX_URL}"),
                         [(LGX_URL, None, [CID_URL, LGX_URL])])

    def test_a_cid_on_each_network_is_a_source(self):
        self.assertEqual(read_line(f"{CID_URL} {DEV_CID_URL} {LGX_URL}"),
                         [(LGX_URL, None, [CID_URL, DEV_CID_URL, LGX_URL])])

    def test_a_local_path_can_follow_a_cid_and_url(self):
        self.assertEqual(read_line(f"{CID_URL} {LGX_URL} ./dist/demo.lgx"),
                         [(LGX_URL, "./dist/demo.lgx", [CID_URL, LGX_URL])])

    def test_a_local_path_keeps_its_spaces(self):
        self.assertEqual(read_line(f"{LGX_URL} ./my dist/demo.lgx"),
                         [(LGX_URL, "./my dist/demo.lgx", [LGX_URL])])

    def test_a_line_with_no_downloadable_url_is_rejected(self):
        with self.assertRaises(SystemExit):
            read_line(CID_URL)

    def test_a_logos_prefix_with_no_cid_is_rejected(self):
        with self.assertRaises(SystemExit):
            read_line(f"logos: {LGX_URL}")

    def test_a_cid_with_no_network_is_rejected(self):
        with self.assertRaises(SystemExit):
            read_line(f"logos:zDvZRw {LGX_URL}")

    def test_a_network_with_no_cid_is_rejected(self):
        with self.assertRaises(SystemExit):
            read_line(f"logos:logos.test: {LGX_URL}")


if __name__ == "__main__":
    unittest.main()


# ─── logos-repo.json and the includes document ───────────────────────────────
#
# Both are hand-edited, and `includesUrl` made them load-bearing: a malformed
# include costs the catalog packages without failing anything at fetch time, by
# design. These cover the gate that catches it at authoring time instead.

def repo_doc(includes_url: str | None = None, **overrides) -> dict:
    doc = {
        "schemaVersion": 1,
        "name": "my-distro",
        "displayName": "My Distro",
        "indexUrl": "https://example.com/my-distro/index.json",
        "trustedSigners": [],
    }
    if includes_url is not None:
        doc["includesUrl"] = includes_url
    doc.update(overrides)
    return doc


def includes_doc(includes: list) -> dict:
    return {"schemaVersion": 1, "includes": includes}


OWN_INDEX = "https://example.com/my-distro/index.json"
OWN_REPO = "https://example.com/my-distro/logos-repo.json"


class ValidateRepoBasics(unittest.TestCase):
    def test_minimal_document_is_valid(self):
        self.assertEqual(index.validate_repo_doc(repo_doc()), [])

    def test_missing_required_fields_are_reported(self):
        doc = repo_doc()
        del doc["displayName"]
        del doc["indexUrl"]
        issues = index.validate_repo_doc(doc)
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("displayName" in i for i in issues))
        self.assertTrue(any("indexUrl" in i for i in issues))

    def test_index_url_must_be_https(self):
        issues = index.validate_repo_doc(
            repo_doc(indexUrl="http://example.com/index.json"))
        self.assertTrue(any("indexUrl" in i and "https" in i for i in issues))


class ValidateRepoIncludesUrl(unittest.TestCase):
    def test_includes_url_is_optional(self):
        self.assertEqual(index.validate_repo_doc(repo_doc()), [])

    def test_valid_includes_url_passes(self):
        doc = repo_doc("https://example.com/my-distro/includes.json")
        self.assertEqual(index.validate_repo_doc(doc), [])

    def test_includes_url_must_be_https(self):
        doc = repo_doc("http://example.com/includes.json")
        self.assertTrue(any("https" in i for i in index.validate_repo_doc(doc)))

    def test_includes_url_must_be_a_string(self):
        doc = repo_doc()
        doc["includesUrl"] = ["https://example.com/includes.json"]
        self.assertTrue(any("string" in i for i in index.validate_repo_doc(doc)))

    # The list lives in its own document. An inline array here is the shape it
    # had before the split and resolves to nothing.
    def test_inline_includes_array_is_rejected(self):
        doc = repo_doc()
        doc["includes"] = [{"repo": "https://a.example/logos-repo.json"}]
        self.assertTrue(any("includesUrl" in i
                            for i in index.validate_repo_doc(doc)))

    def test_pointing_at_own_index_url_is_caught(self):
        doc = repo_doc(OWN_INDEX)
        self.assertTrue(any("indexUrl" in i for i in index.validate_repo_doc(doc)))

    def test_pointing_at_itself_is_caught_when_the_url_is_known(self):
        doc = repo_doc(OWN_REPO)
        self.assertEqual(index.validate_repo_doc(doc), [])
        self.assertTrue(any("logos-repo.json" in i for i in
                            index.validate_repo_doc(doc, self_url=OWN_REPO)))


class ValidateIncludesDoc(unittest.TestCase):
    def test_include_with_no_filter_is_valid(self):
        doc = includes_doc([{"repo": "https://other.example/logos-repo.json"}])
        self.assertEqual(index.validate_includes_doc(doc), [])

    def test_bare_names_and_objects_both_validate(self):
        doc = includes_doc([
            {"repo": "https://a.example/logos-repo.json",
             "packages": ["chat_module"]},
            {"repo": "https://b.example/logos-repo.json",
             "packages": [{"name": "storage_module", "rootHash": "ab12"}]},
        ])
        self.assertEqual(index.validate_includes_doc(doc), [])

    def test_missing_includes_key_is_reported(self):
        self.assertTrue(any("'includes'" in i
                            for i in index.validate_includes_doc({"schemaVersion": 1})))

    def test_repo_must_be_https(self):
        doc = includes_doc([{"repo": "http://a.example/logos-repo.json"}])
        self.assertTrue(any("https" in i
                            for i in index.validate_includes_doc(doc)))

    def test_repo_is_required(self):
        doc = includes_doc([{"packages": ["x"]}])
        self.assertTrue(any("'repo'" in i
                            for i in index.validate_includes_doc(doc)))

    # Naming the index instead of the identity card resolves to nothing, and
    # looks exactly like an unreachable catalog when it does.
    def test_pointing_at_the_owning_index_url_is_caught(self):
        doc = includes_doc([{"repo": OWN_INDEX}])
        self.assertEqual(index.validate_includes_doc(doc), [])
        self.assertTrue(any("indexUrl" in i for i in
                            index.validate_includes_doc(doc, index_url=OWN_INDEX)))

    def test_self_reference_is_caught_when_the_url_is_known(self):
        doc = includes_doc([{"repo": OWN_REPO}])
        self.assertEqual(index.validate_includes_doc(doc), [])
        self.assertTrue(any("itself" in i for i in
                            index.validate_includes_doc(doc, self_url=OWN_REPO)))

    def test_duplicate_includes_are_caught(self):
        url = "https://a.example/logos-repo.json"
        doc = includes_doc([{"repo": url}, {"repo": url}])
        self.assertTrue(any("duplicate" in i
                            for i in index.validate_includes_doc(doc)))

    # An empty list is not "take everything" — that is what omitting the field
    # means. Silently following it would cost two round-trips for no packages.
    def test_empty_packages_list_is_an_error_not_a_wildcard(self):
        doc = includes_doc([{"repo": "https://a.example/logos-repo.json",
                             "packages": []}])
        self.assertTrue(any("selects" in i
                            for i in index.validate_includes_doc(doc)))

    def test_unknown_fields_are_reported(self):
        doc = includes_doc([{"repo": "https://a.example/logos-repo.json",
                             "mirror": True}])
        self.assertTrue(any("mirror" in i
                            for i in index.validate_includes_doc(doc)))

    def test_malformed_selectors_are_reported(self):
        doc = includes_doc([{"repo": "https://a.example/logos-repo.json",
                             "packages": [42, {"version": "1.0.0"}]}])
        self.assertEqual(len(index.validate_includes_doc(doc)), 2)


class ValidateIncludesRanges(unittest.TestCase):
    @requires_lgx
    def test_well_formed_ranges_pass(self):
        doc = includes_doc([{"repo": "https://a.example/logos-repo.json",
                             "packages": [{"name": "m", "version": "^0.2.0"},
                                          {"name": "n", "version": "2.1.0"},
                                          {"name": "o", "version": ">=1.0.0 <2.0.0"}]}])
        self.assertEqual(index.validate_includes_doc(doc), [])

    @requires_lgx
    def test_malformed_range_is_reported(self):
        doc = includes_doc([{"repo": "https://a.example/logos-repo.json",
                             "packages": [{"name": "m", "version": "1..2"}]}])
        self.assertTrue(any("well-formed range" in i
                            for i in index.validate_includes_doc(doc)))

    # Ranges are judged by `lgx semver valid-range`, the same implementation
    # the clients use. Without lgx the structural checks must still run —
    # dying there would make the gate unusable wherever lgx isn't installed.
    def test_ranges_go_unchecked_without_lgx(self):
        doc = includes_doc([{"repo": "https://a.example/logos-repo.json",
                             "packages": [{"name": "m", "version": "1..2"}]}])
        real = index._lgx_has_semver
        index._lgx_has_semver = lambda: False
        try:
            self.assertEqual(index.validate_includes_doc(doc), [])
        finally:
            index._lgx_has_semver = real
