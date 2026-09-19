import os
import unittest
from unittest.mock import patch

import enroll
import provision as core


def application(body=None):
    return {"number": 1, "user": {"login": "Student-123", "type": "User"},
            "body": body or "### 课程\n\n2073 · 专业阶段 - rCore-Tutorial\n"}


class ProvisionServer:
    """Deterministic API fault injection; real GitHub runs are recorded separately."""
    def __init__(self):
        self.final = "LearningOS/2026a-rcore-Student-123"
        self.preparing = "LearningOS/preparing-2026a-rcore-Student-123"
        self.template = "LearningOS/2026a-rcore"
        self.created = self.published = False
        self.lose_create = self.lose_rename = self.lose_variable = False
        self.variable = None
        self.visibility = "all"
        self.calls = []
        self.check_position = None

    def check(self, repository):
        self.check_position = len(self.calls)
        return "https://github.com/" + repository + "/actions/runs/123"

    def __call__(self, method, path, data=None, **options):
        self.calls.append((method, path, data))
        if path == "users/Student-123":
            return {"type": "User", "login": "Student-123"}
        if path == "repos/LearningOS/2026a-rcore":
            return {"is_template": True, "private": False}
        if "/actions/secrets/" in path:
            return {"visibility": self.visibility}
        if "/branches?" in path:
            return [{"name": branch} for branch in enroll.COURSES["2073"]["branches"]]
        if path.endswith("/generate"):
            self.created = True
            if self.lose_create:
                raise core.GitHubError("Created but response lost; retry got HTTP 422", 422)
            return {"id": 123}
        if path.endswith("/variables/STUDENT_GITHUB"):
            return self.variable
        if path.endswith("/variables") and method == "POST":
            self.variable = {"value": "Someone-Else" if self.lose_variable else data["value"]}
            if self.lose_variable:
                raise core.GitHubError("Variable response lost", 502, temporary=True)
            return None
        if method == "PATCH" and path == "repos/" + self.preparing:
            self.published = True
            if self.lose_rename:
                raise core.GitHubError("Rename response lost", temporary=True)
            return {"id": 123}
        if path in ["repos/" + self.final, "repos/" + self.preparing]:
            exists = self.published if path.endswith(self.final) else self.created
            return {"id": 123, "template_repository": {"full_name": self.template}, "private": False} if exists else None
        return None


class EnrollmentTests(unittest.TestCase):
    def test_rename_respects_long_rate_limit_without_outer_early_retry(self):
        server = ProvisionServer()
        server.created = True
        writes = []
        def limited(method, path, data=None, **options):
            if method == "PATCH":
                writes.append(path)
                raise core.GitHubError("HTTP 429: wait 3600 seconds", 429, True, 3600)
            return server(method, path, data, **options)
        with patch.object(core, "api", side_effect=limited), patch.object(core.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "no early retry"):
                core.publish_repository(server.preparing, server.final, 123)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(server.calls), 2)  # No GET after a long rate-limit rejection.
        sleep.assert_not_called()

    def test_archived_repository_is_rejected_before_writes(self):
        with self.assertRaisesRegex(ValueError, "archived"):
            core.validate_source({"archived": True}, "org/template")

    def test_notification_failure_keeps_the_ready_repository_link(self):
        calls = []
        def notify(method, path, data=None, **options):
            calls.append((method, path, data))
            if len(calls) == 1:
                raise RuntimeError("Comment response lost")
        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}), \
                patch.object(enroll, "provision", return_value=("https://github.com/ready", "https://github.com/check")), \
                patch.object(enroll, "api", side_effect=notify):
            with self.assertRaisesRegex(RuntimeError, "Comment response lost"):
                enroll.process_application(application(), "https://github.com/run")
        self.assertIn("已准备完成", calls[-1][2]["body"])
        self.assertIn("https://github.com/ready", calls[-1][2]["body"])
        self.assertNotIn("本次领取未完成", calls[-1][2]["body"])

    def test_all_form_choices_map_to_fixed_catalog(self):
        for course_id, course in enroll.COURSES.items():
            for choice in (course["title"], f"{course_id} · {course['title']}"):
                with self.subTest(choice=choice):
                    issue = application(f"### 课程\n\n{choice}\n")
                    login, selected, config = enroll.parse_request(issue)
                    self.assertEqual((login, selected), ("Student-123", course_id))
                    self.assertEqual(config, course)

    def test_student_identity_only_comes_from_issue_author(self):
        issue = application("### 课程\n\n2073 · 专业阶段 - rCore-Tutorial\n"
                            "\n### GitHub 登录名\n\nMaintainer\n$(touch unwanted)\n")
        self.assertEqual(enroll.parse_request(issue)[0], "Student-123")

    def test_unknown_or_ambiguous_course_rejected(self):
        for body in ["### 课程\n\n9999 · Other", "hello",
                     "### 课程\n\n2073 · 专业阶段 - rCore-Tutorial; echo unsafe",
                     application()["body"] * 2]:
            with self.assertRaises(ValueError):
                enroll.parse_request(application(body))

    def test_bot_and_pull_request_rejected(self):
        issue = application()
        issue["user"]["type"] = "Bot"
        with self.assertRaises(ValueError):
            enroll.parse_request(issue)
        issue = application()
        issue["pull_request"] = {}
        with self.assertRaises(ValueError):
            enroll.parse_request(issue)

    def test_new_repository_is_published_only_after_configuration_passes(self):
        server = ProvisionServer()
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            url, check_url = core.provision("Student-123", "2073", enroll.COURSES["2073"])
        self.assertEqual(url, "https://github.com/" + server.final)
        self.assertEqual(check_url, url + "/actions/runs/123")
        writes = [(method, path, data) for method, path, data in server.calls if method != "GET"]
        generate = next(data for method, path, data in writes if path.endswith("/generate"))
        self.assertEqual(generate["name"], server.preparing.split("/", 1)[1])
        self.assertTrue(generate["include_all_branches"])
        self.assertFalse(generate["private"])
        self.assertEqual(writes[-1][0], "PATCH")
        self.assertEqual(writes[-1][2]["name"], server.final.split("/", 1)[1])
        self.assertLess(server.check_position, len(server.calls) - 1)
        self.assertTrue(server.published)

    def test_failed_configuration_does_not_publish_or_invite(self):
        server = ProvisionServer()
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=core.ConfigurationError("Failed", "https://github.com/check")):
            with self.assertRaises(core.ConfigurationError):
                core.provision("Student-123", "2073", enroll.COURSES["2073"])
        self.assertFalse(server.published)
        self.assertFalse(any("/collaborators/" in path for _, path, _ in server.calls))

    def test_bad_secret_preflight_creates_no_repository(self):
        server = ProvisionServer()
        server.visibility = "private"
        with patch.object(core, "api", side_effect=server):
            with self.assertRaisesRegex(ValueError, "public repositories"):
                core.provision("Student-123", "2073", enroll.COURSES["2073"])
        self.assertFalse(server.created)
        self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_lost_creation_and_rename_responses_recover_same_repository(self):
        server = ProvisionServer()
        server.lose_create = server.lose_rename = True
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            url, _ = core.provision("Student-123", "2073", enroll.COURSES["2073"])
        self.assertEqual(url, "https://github.com/" + server.final)
        self.assertTrue(server.published)
        self.assertEqual(sum(path.endswith("/generate") for _, path, _ in server.calls), 1)
        self.assertEqual(sum(method == "PATCH" for method, _, _ in server.calls), 1)

    def test_existing_formal_repository_is_not_renamed(self):
        server = ProvisionServer()
        server.created = server.published = True
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            core.provision("Student-123", "2073", enroll.COURSES["2073"])
        self.assertFalse(any(method == "PATCH" or path.endswith("/generate") for method, path, _ in server.calls))

    def test_conflicting_final_name_is_never_overwritten(self):
        with patch.object(core, "api", return_value={"id": 999}) as api:
            with self.assertRaisesRegex(ValueError, "occupied"):
                core.publish_repository("org/preparing-student", "org/student", 123)
        self.assertEqual(api.call_count, 1)

    def test_lost_variable_response_does_not_overwrite_other_identity(self):
        server = ProvisionServer()
        server.lose_variable = True
        with patch.object(core, "api", side_effect=server), patch.object(core, "check_configuration") as check:
            with self.assertRaisesRegex(ValueError, "another student"):
                core.provision("Student-123", "2073", enroll.COURSES["2073"])
        check.assert_not_called()
        self.assertFalse(server.published)

    def test_configuration_waits_for_exact_dispatched_run(self):
        calls = []
        responses = [None, {"status": "queued"}, {"status": "completed", "conclusion": "success"}]

        def fake_api(method, path, data=None, **options):
            calls.append((method, path, data))
            if path.endswith("/dispatches"):
                self.assertTrue(data["return_run_details"])
                return {"workflow_run_id": 123}
            if path.endswith("/jobs"):
                return {"jobs": [{"name": "configuration", "conclusion": "success"}]}
            self.assertTrue(path.endswith("/actions/runs/123"))
            return responses.pop(0)

        with patch.object(core, "api", side_effect=fake_api), patch.object(core.time, "sleep"):
            url = core.check_configuration("org/student")
        self.assertEqual(url, "https://github.com/org/student/actions/runs/123")
        self.assertEqual(len(calls), 5)

    def test_configuration_failure_or_skipped_job_is_not_success(self):
        cases = [({"status": "completed", "conclusion": "failure"}, None),
                 ({"status": "completed", "conclusion": "success"},
                  {"jobs": [{"name": "configuration", "conclusion": "skipped"}]})]
        for run, jobs in cases:
            with patch.object(core, "api", side_effect=[{"workflow_run_id": 123}, run, jobs]):
                with self.assertRaises(core.ConfigurationError):
                    core.check_configuration("org/student")

    def test_configuration_timeout_remains_an_error(self):
        with patch.object(core, "api", side_effect=[{"workflow_run_id": 123}, {"status": "queued"}]), \
                patch.object(core.time, "monotonic", side_effect=[0, 0, 601]), \
                patch.object(core.time, "sleep"):
            with self.assertRaisesRegex(core.ConfigurationError, "10 minutes"):
                core.check_configuration("org/student")

    def test_failed_configuration_never_closes_issue_or_sends_success(self):
        error = core.ConfigurationError("Secret missing", "https://github.com/org/repo/actions/runs/1")
        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}), \
                patch.object(enroll, "provision", side_effect=error), patch.object(enroll, "api") as api:
            with self.assertRaises(core.ConfigurationError):
                enroll.process_application(application(), "https://github.com/run")
        payloads = [call.args[2] for call in api.call_args_list]
        self.assertIn({"state": "open"}, payloads)
        self.assertNotIn({"state": "closed"}, payloads)
        self.assertFalse(any("仓库已配置" in payload.get("body", "") for payload in payloads))

    def test_notification_error_does_not_hide_original_failure(self):
        original = ValueError("Original configuration failure")
        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}), \
                patch.object(enroll, "provision", side_effect=original), \
                patch.object(enroll, "api", side_effect=RuntimeError("Issue API also failed")):
            with self.assertRaisesRegex(ValueError, "Original configuration failure"):
                enroll.process_application(application(), "https://github.com/run")

    def test_success_comment_and_close_follow_configuration_pass(self):
        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}), \
                patch.object(enroll, "provision", return_value=("https://github.com/repo", "https://github.com/check")), \
                patch.object(enroll, "api") as api:
            enroll.process_application(application(), "https://github.com/run")
        self.assertIn("本次配置检查已通过", api.call_args_list[0].args[2]["body"])
        self.assertEqual(api.call_args_list[-1].args[2], {"state": "closed"})

    def test_existing_different_repository_is_not_modified(self):
        server = ProvisionServer()
        server.created = server.published = True
        server.template = "someone/else"
        with patch.object(core, "api", side_effect=server):
            with self.assertRaisesRegex(ValueError, "left untouched"):
                core.provision("Student-123", "2073", enroll.COURSES["2073"])
        self.assertTrue(all(method == "GET" for method, _, _ in server.calls))


if __name__ == "__main__":
    unittest.main()
