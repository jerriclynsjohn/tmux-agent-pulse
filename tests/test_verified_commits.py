"""Exercise the exact trusted verifier embedded in the workflow, without GitHub writes."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import types
import unittest
from unittest.mock import Mock
import urllib.error

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/verified-commits.yml"
text = WORKFLOW.read_text()
start = "          python3 - <<'PY'\n"
source = text.split(start, 1)[1].split("          PY\n", 1)[0]
source = "\n".join(line[10:] for line in source.splitlines())
verifier = types.ModuleType("trusted_signature_workflow")
exec(compile(source, str(WORKFLOW), "exec"), verifier.__dict__)

REPOSITORY = "fixture/project"
BASE = "b" * 40


class FakeGitHub:
    def __init__(self, count=2):
        self.commits = [{"sha": f"{index + 1:040x}",
                         "commit": {"verification": {"verified": True}}} for index in range(count)]
        self.head = self.commits[-1]["sha"] if self.commits else "a" * 40
        self.pr = {"state": "open", "commits": count, "head": {"sha": self.head},
                   "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPOSITORY}}}
        self.event = {"number": 1, "pull_request": deepcopy(self.pr)}
        self.statuses, self.paths = [], []
        self.pr_reads = 0
        self.mutate = None

    def request(self, path, data=None):
        self.paths.append(path)
        if self.mutate:
            result = self.mutate(path, data)
            if result is not None:
                return result
        if data is not None:
            self.statuses.append((path, deepcopy(data)))
            return deepcopy(data)
        if path.endswith("/pulls/1"):
            self.pr_reads += 1
            return deepcopy(self.pr)
        page = int(path.rsplit("page=", 1)[1])
        batch = deepcopy(self.commits[(page - 1) * 100:page * 100])
        if "/compare/" in path:
            return {"total_commits": len(self.commits), "base_commit": {"sha": BASE}, "commits": batch}
        return batch


class VerifiedCommitsTests(unittest.TestCase):
    def run_verifier(self, api):
        with redirect_stdout(io.StringIO()):
            return verifier.run(api, REPOSITORY, api.event, "https://github.com/fixture/project/actions/runs/1")

    def assert_blocked(self, api):
        self.assertEqual(self.run_verifier(api), 1)
        self.assertEqual(api.statuses[-1][1]["state"], "failure")
        self.assertTrue(all(path.endswith(api.event["pull_request"]["head"]["sha"]) for path, _ in api.statuses))

    def test_paginates_all_commits_and_publishes_on_exact_pr_head(self):
        api = FakeGitHub(201)
        self.assertEqual(self.run_verifier(api), 0)
        self.assertEqual([item[1]["state"] for item in api.statuses], ["pending", "success"])
        self.assertTrue(all(item[1]["context"] == "Verified commits" for item in api.statuses))
        self.assertTrue(all(path.endswith(api.head) for path, _ in api.statuses))
        self.assertEqual(len([path for path in api.paths if "/commits?" in path]), 3)

    def test_signed_head_does_not_hide_unsigned_earlier_commit(self):
        api = FakeGitHub()
        api.commits[0]["commit"]["verification"]["verified"] = False
        self.assert_blocked(api)
        self.assertNotIn("success", [item[1]["state"] for item in api.statuses])

    def test_missing_or_non_boolean_verification_never_passes(self):
        for value in ({}, {"verified": None}, {"verified": "true"}, {"verified": 1}, None):
            with self.subTest(value=value):
                api = FakeGitHub()
                api.commits[0]["commit"]["verification"] = value
                self.assert_blocked(api)

    def test_over_limit_empty_and_invalid_count_fail_closed(self):
        for count in (251, 0, "2", True):
            with self.subTest(count=count):
                api = FakeGitHub()
                api.pr["commits"] = count
                self.assert_blocked(api)
                self.assertFalse(any("/commits?" in path for path in api.paths))

    def test_missing_page_and_duplicate_commits_fail_closed(self):
        for mode in ("missing", "duplicate"):
            with self.subTest(mode=mode):
                api = FakeGitHub(101)
                if mode == "duplicate":
                    api.commits[-1] = deepcopy(api.commits[0])
                else:
                    api.mutate = lambda path, data: [] if "/commits?" in path and path.endswith("page=2") else None
                self.assert_blocked(api)

    def test_immutable_comparison_detects_a_mixed_pr_page(self):
        api = FakeGitHub()
        def mutate(path, data):
            if "/compare/" in path:
                altered = deepcopy(api.commits)
                altered[0]["sha"] = "c" * 40
                return {"total_commits": 2, "base_commit": {"sha": BASE}, "commits": altered}
        api.mutate = mutate
        self.assert_blocked(api)

    def test_stale_head_or_retargeting_resets_previous_success_then_blocks(self):
        for field, value in (("head", "c" * 40), ("base", "d" * 40), ("ref", "other")):
            with self.subTest(field=field):
                api = FakeGitHub()
                if field == "ref":
                    api.pr["base"]["ref"] = value
                else:
                    api.pr[field]["sha"] = value
                self.assert_blocked(api)
                self.assertEqual(api.statuses[0][1]["state"], "pending")

    def test_change_during_audit_is_rejected_before_success_publication(self):
        api = FakeGitHub()
        def mutate(path, data):
            if path.endswith("/pulls/1") and api.pr_reads >= 1:
                api.pr["head"]["sha"] = "d" * 40
        api.mutate = mutate
        self.assert_blocked(api)
        self.assertNotIn("success", [item[1]["state"] for item in api.statuses])

    def test_failed_status_post_never_reports_success(self):
        for rejected_state in ("pending", "success", "failure"):
            with self.subTest(rejected_state=rejected_state):
                api = FakeGitHub()
                if rejected_state == "failure":
                    api.commits[0]["commit"]["verification"]["verified"] = False
                def mutate(path, data):
                    if data is not None and data["state"] == rejected_state:
                        raise verifier.VerificationError("GitHub API returned HTTP 503")
                api.mutate = mutate
                self.assertEqual(self.run_verifier(api), 1)
                self.assertNotIn("success", [item[1]["state"] for item in api.statuses])

    def test_api_error_and_malformed_response_cannot_leave_success(self):
        for failure in (verifier.VerificationError("GitHub API returned HTTP 503"), {"unexpected": True}):
            with self.subTest(failure=failure):
                api = FakeGitHub()
                def mutate(path, data):
                    if "/commits?" in path:
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                api.mutate = mutate
                self.assert_blocked(api)

    def test_transport_errors_do_not_expose_response_body_or_token(self):
        api = verifier.GitHub("fixture-token")
        error = urllib.error.HTTPError("https://api.github.com/", 403, "private response", {}, None)
        api.opener.open = Mock(side_effect=error)
        with self.assertRaisesRegex(verifier.VerificationError, "^GitHub API returned HTTP 403$"):
            api.request("/repos/fixture/project/pulls/1")
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/fixture/project/pulls/1")
        self.assertIsNone(verifier.NoRedirect().redirect_request(request, None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()
