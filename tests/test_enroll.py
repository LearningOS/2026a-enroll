import os
import json
import unittest
from unittest.mock import patch

import enroll
import provision as core


def application(body=None):
    return {"number": 1, "user": {"login": "Student-123", "type": "User", "id": 456},
            "body": body or "### 课程\n\n导学阶段-Rust 语言基础\n"}


class ProvisionServer:
    """Deterministic API fault injection; real GitHub runs are recorded separately."""
    def __init__(self):
        self.final = "LearningOS/2026a-rcore-Student-123"
        self.preparing = "LearningOS/preparing-2026a-rcore-Student-123"
        self.template = "LearningOS/2026a-rcore"
        self.created = self.published = False
        self.lose_create = self.lose_rename = self.lose_variable = False
        self.variable = None
        self.can_write = False
        self.owner_id = None
        self.description = ""
        self.invites = []
        self.invitee = {"id": 456, "login": "Student-123", "type": "User"}
        self.visibility = "all"
        self.calls = []
        self.check_position = None

    def check(self, repository):
        self.check_position = len(self.calls)
        return "https://github.com/" + repository + "/actions/runs/123"

    def __call__(self, method, path, data=None, **options):
        self.calls.append((method, path, data))
        if path == "users/Student-123":
            return self.invitee
        if path == "repos/LearningOS/2026a-rcore":
            return {"is_template": True, "private": False}
        if "/actions/secrets/" in path:
            return {"visibility": self.visibility}
        if "/branches?" in path:
            return [{"name": branch} for branch in enroll.COURSES["2073"]["branches"]]
        if path.endswith("/generate"):
            self.created = True
            self.description = data["description"]
            if self.lose_create:
                raise core.GitHubError("Created but response lost; retry got HTTP 422", 422)
            return {"id": 123}
        if path.endswith("/variables/STUDENT_GITHUB"):
            if self.variable is None and not options.get("missing_ok"):
                raise core.GitHubError("Not Found", 404)
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
            return {"id": 123, "description": self.description, "full_name": path[6:], "template_repository": {"full_name": self.template},
                    "private": False} if exists else None
        if path == "repositories/123/properties/values":
            if method == "PATCH":
                self.owner_id = data["properties"][0]["value"]
                return
            return [{"property_name": core.OWNER_PROPERTY, "value": self.owner_id}]
        if path == "repositories/123/collaborators/Student-123/permission":
            return {"user": self.invitee, "permission": "write" if self.can_write else "read"}
        if path.startswith("repositories/123/invitations?"):
            page = int(path.split("page=")[-1])
            return self.invites[(page-1)*100:page*100]
        if path.startswith("repositories/123/invitations/") and method == "DELETE":
            self.invites = [i for i in self.invites if i["id"] != int(path.split("/")[-1])]
            return None
        if path == "repositories/123/collaborators/Student-123" and method == "PUT":
            self.invites.append(self.invitation(42))
            return None
        if method == "PUT" and path.endswith("/enable"):
            return None
        raise AssertionError(f"Unexpected {method} {path}")

    def invitation(self, invite_id, student_id=456, expired=False):
        return {"id": invite_id, "invitee": {"id": student_id}, "repository": {"id": 123},
                "permissions": "write", "expired": expired}

    def enroll(self):
        return core.provision("Student-123", "2073", enroll.COURSES["2073"], self.invitee["id"])


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        token = patch.dict(os.environ, {"GH_TOKEN": "test-enrollment-credential"})
        token.start()
        self.addCleanup(token.stop)

    def test_recycled_login_cannot_claim_bound_or_unproven_legacy_repository(self):
        for owner in ("123456", None):
            server = ProvisionServer()
            server.created = server.published = True
            server.variable = {"value": server.invitee["login"]}
            server.owner_id = owner
            def request(method, path, data=None, **options):
                if path.startswith("search/issues?"):
                    return {"items": []}
                return server(method, path, data, **options)
            with self.subTest(owner=owner), patch.object(core, "api", side_effect=request):
                with self.assertRaisesRegex(ValueError, "GitHub.*ID"):
                    server.enroll()
            self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_legacy_receipt_requires_original_author_bot_and_matching_run(self):
        repo = {"id": 123, "full_name": "LearningOS/2026a-rcore-Student-123"}
        student = {"id": 456, "login": "Student-123"}
        body = "[本次配置检查已通过](https://github.com/" + repo["full_name"] + "/actions/runs/77)"
        for author, bot, rid, expected in ((456, 41898282, 123, True), (789, 41898282, 123, False),
                                           (456, 555, 123, False), (456, 41898282, 321, False)):
            def request(method, path, **options):
                if path.endswith("/issues/4"):
                    return {"number": 4, "user": {"id": author}}
                if "/comments?" in path:
                    return [{"user": {"id": bot, "login": "github-actions[bot]"}, "body": body}]
                if path == "repositories/123/actions/runs/77":
                    return {"repository": {"id": rid}, "event": "workflow_dispatch",
                            "path": ".github/workflows/check-config.yml", "conclusion": "success"}
                self.assertTrue(path.startswith("search/issues?"))
                return {"items": []}
            with self.subTest(author=author, bot=bot, rid=rid), patch.object(core, "api", side_effect=request):
                self.assertEqual(core.legacy_receipt(repo, student, 4), expected)

    def test_formal_repository_missing_required_branch_is_not_certified(self):
        server = ProvisionServer()
        server.created = server.published = True
        server.owner_id = "456"
        def request(method, path, data=None, **options):
            if path.startswith("repos/" + server.final + "/branches?"):
                return [{"name": "main"}]
            return server(method, path, data, **options)
        with patch.object(core, "api", side_effect=request), self.assertRaisesRegex(ValueError, "missing.*ch8"):
            server.enroll()
        self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_withdrawal_during_configuration_stops_invitation_and_publication(self):
        server = ProvisionServer()
        def withdrawn():
            raise enroll.Withdrawn()
        with patch.object(core, "api", side_effect=server), patch.object(core, "check_configuration",
                side_effect=server.check), self.assertRaises(enroll.Withdrawn):
            core.provision("Student-123", "2073", enroll.COURSES["2073"], 456, 1, withdrawn)
        self.assertFalse(server.published)
        self.assertFalse(server.invites)

    def test_queued_automatic_request_honors_withdrawal_manual_retry_can_recover(self):
        snapshot = dict(application(), state="open")
        current = dict(snapshot, state="closed")
        event = json.dumps({"action": "opened", "issue": snapshot})
        for kind in ("issues", "workflow_dispatch"):
            env = {"GITHUB_REPOSITORY": enroll.HUB, "GITHUB_EVENT_PATH": "unused", "GITHUB_RUN_ID": "1",
                   "GITHUB_EVENT_NAME": kind, "RETRY_ISSUE_NUMBER": "1"}
            with self.subTest(kind=kind), patch.dict(os.environ, env), patch.object(enroll.Path, "read_text",
                    return_value=event), patch.object(enroll, "api", return_value=current) as api, patch.object(
                    enroll, "process_application") as process:
                enroll.main()
                api.assert_called_once_with("GET", "repos/" + enroll.HUB + "/issues/1", issue=True)
                if kind == "issues":
                    process.assert_not_called()
                else:
                    self.assertEqual(process.call_args.args[0], current)
                    self.assertIsNone(process.call_args.args[2])
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
                patch.object(enroll, "provision", return_value=("https://github.com/ready", "https://github.com/check", "pending")), \
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
                    if course.get("enrollment_open") is not True:
                        with self.assertRaises(enroll.EnrollmentClosed):
                            enroll.parse_request(issue)
                        continue
                    login, selected, config = enroll.parse_request(issue)
                    self.assertEqual((login, selected), ("Student-123", course_id))
                    self.assertEqual(config, course)

    def test_student_identity_only_comes_from_issue_author(self):
        issue = application("### 课程\n\n导学阶段-Rust 语言基础\n"
                            "\n### GitHub 登录名\n\nMaintainer\n$(touch unwanted)\n")
        self.assertEqual(enroll.parse_request(issue)[0], "Student-123")

    def test_closed_courses_never_provision_even_with_legacy_form_bodies(self):
        for course_id, course in enroll.COURSES.items():
            closed_course = dict(course, enrollment_open=False)
            for choice in (course['title'], f"{course_id} · {course['title']}"):
                with self.subTest(choice=choice), patch.dict(enroll.COURSES, {course_id: closed_course}), \
                        patch.object(enroll, 'provision') as provision, patch.object(enroll, 'api') as api:
                    enroll.process_application(application(f"### 课程\n\n{choice}\n"), 'https://github.com/run')
                    provision.assert_not_called()
                    self.assertIn('暂未开放', api.call_args_list[0].args[2]['body'])
                    self.assertEqual(api.call_args_list[-1].args[2], {'state': 'closed', 'state_reason': 'not_planned'})

    def test_unconfigured_enrollment_is_closed_by_default(self):
        course = dict(enroll.COURSES['2084'])
        course.pop('enrollment_open')
        with patch.dict(enroll.COURSES, {'2084': course}):
            with self.assertRaises(enroll.EnrollmentClosed):
                enroll.parse_request(application())

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
            url, check_url, access = server.enroll()
        self.assertEqual(access, "pending")
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
                server.enroll()
        self.assertFalse(server.published)
        self.assertFalse(any("/collaborators/" in path for _, path, _ in server.calls))

    def test_bad_secret_preflight_creates_no_repository(self):
        server = ProvisionServer()
        server.visibility = "private"
        with patch.object(core, "api", side_effect=server):
            with self.assertRaisesRegex(ValueError, "public repositories"):
                server.enroll()
        self.assertFalse(server.created)
        self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_selected_course_secret_is_granted_before_configuration_check(self):
        server = ProvisionServer()
        server.visibility = "selected"
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            server.enroll()
        grants = [(index, method, path) for index, (method, path, _) in enumerate(server.calls)
                  if "/actions/secrets/" in path and method == "PUT"]
        self.assertEqual(len(grants), 1)
        index, _, path = grants[0]
        self.assertEqual(path, "orgs/LearningOS/actions/secrets/OSCAMP_2026A_RCORE_TOKEN/repositories/123")
        self.assertLess(index, server.check_position)
        self.assertTrue(server.published)

    def test_failed_secret_grant_does_not_check_publish_or_invite(self):
        server = ProvisionServer()
        server.visibility = "selected"
        def denied(method, path, data=None, **options):
            if "/actions/secrets/" in path and method == "PUT":
                raise core.GitHubError("Secret grant denied", 403)
            return server(method, path, data, **options)
        with patch.object(core, "api", side_effect=denied), patch.object(core, "check_configuration") as check:
            with self.assertRaisesRegex(core.GitHubError, "Secret grant denied"):
                server.enroll()
        check.assert_not_called()
        self.assertFalse(server.published)
        self.assertFalse(any("/collaborators/" in path for _, path, _ in server.calls))

    def test_lost_creation_and_rename_responses_recover_same_repository(self):
        server = ProvisionServer()
        server.lose_create = server.lose_rename = True
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            url, _, _ = server.enroll()
        self.assertEqual(url, "https://github.com/" + server.final)
        self.assertTrue(server.published)
        self.assertEqual(sum(path.endswith("/generate") for _, path, _ in server.calls), 1)
        self.assertEqual(sum(method == "PATCH" and path == "repos/" + server.preparing
                             for method, path, _ in server.calls), 1)

    def test_existing_formal_repository_is_not_renamed(self):
        server = ProvisionServer()
        server.created = server.published = True
        server.owner_id = "456"
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            server.enroll()
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
                server.enroll()
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
            self.assertTrue(path.endswith("/actions/runs/123/attempts/1"))
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
        self.assertNotIn({"state": "open"}, payloads)
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
                patch.object(enroll, "provision", return_value=("https://github.com/repo", "https://github.com/check", "write")), \
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
                server.enroll()
        self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_resume_the_same_repository_after_interrupted_configuration(self):
        # #578 and #583: the write took effect, but the subsequent Actions endpoint was not ready.
        for step in ("variables/STUDENT_GITHUB", "workflows/check-config.yml/enable"):
            with self.subTest(step=step):
                server = ProvisionServer()
                def outage(method, path, data=None, **options):
                    if path.endswith(step) and (method == "PUT" or not options.get("missing_ok")):
                        raise core.GitHubError("Not Found", 404)
                    return server(method, path, data, **options)
                with patch.object(core, "api", side_effect=outage), patch.object(core.time, "sleep"):
                    with self.assertRaisesRegex(core.GitHubError, "Not Found"):
                        server.enroll()
                self.assertTrue(server.created)
                self.assertFalse(server.published)
                self.assertEqual(server.variable, {"value": "Student-123"})
                with patch.object(core, "api", side_effect=server), patch.object(
                        core, "check_configuration", side_effect=server.check):
                    self.assertEqual(server.enroll()[2], "pending")
                self.assertTrue(server.published)
                for suffix in ("/generate", "/actions/variables"):
                    self.assertEqual(sum(method == "POST" and path.endswith(suffix)
                                         for method, path, _ in server.calls), 1)

    def test_transient_variable_and_workflow_404_recover_in_one_application(self):
        server = ProvisionServer()
        failures = {"GET": 2, "PUT": 2}
        def not_visible(method, path, data=None, **options):
            if ((path.endswith("STUDENT_GITHUB") and not options.get("missing_ok")
                 or path.endswith("check-config.yml/enable")) and failures[method]):
                failures[method] -= 1
                raise core.GitHubError("Not Found", 404)
            return server(method, path, data, **options)
        with patch.object(core, "api", side_effect=not_visible), patch.object(core.time, "sleep"), \
                patch.object(core, "check_configuration", side_effect=server.check):
            self.assertEqual(server.enroll()[2], "pending")
        self.assertEqual(failures, {"GET": 0, "PUT": 0})

    def test_partially_generated_branch_set_is_neither_published_nor_recreated(self):
        server = ProvisionServer()
        def incomplete(method, path, data=None, **options):
            if path.startswith("repos/" + server.preparing + "/branches?"):
                return [{"name": "main"}]
            return server(method, path, data, **options)
        with patch.object(core, "api", side_effect=incomplete), patch.object(core.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "missing branches.*ch3"):
                server.enroll()
        self.assertFalse(server.published)
        self.assertIsNone(server.variable)
        with patch.object(core, "api", side_effect=server), patch.object(
                core, "check_configuration", side_effect=server.check):
            self.assertEqual(server.enroll()[2], "pending")
        self.assertEqual(sum(path.endswith("/generate") for _, path, _ in server.calls), 1)

    def test_issue_1068_waits_for_the_same_attempt_without_redispatch(self):
        run = {"status": "completed", "conclusion": "success"}
        job = {"name": "configuration", "conclusion": "success"}
        with patch.object(core, "api", side_effect=[{"workflow_run_id": 123}, run, None,
                          run, {"jobs": []}, run, {"jobs": [job]}]) as api, patch.object(core.time, "sleep"):
            core.check_configuration("org/student")
        self.assertEqual([c.args[0] for c in api.call_args_list].count("POST"), 1)
        self.assertEqual({c.args[1] for c in api.call_args_list[1:]},
                         {"repos/org/student/actions/runs/123/attempts/1",
                          "repos/org/student/actions/runs/123/attempts/1/jobs"})

    def test_existing_write_or_pending_invitation_needs_no_access_write(self):
        for state in ("write", "pending"):
            server = ProvisionServer()
            server.can_write = state == "write"
            server.invites = [server.invitation(7)]
            with self.subTest(state=state), patch.object(core, "api", side_effect=server):
                self.assertEqual(core.ensure_access(123, server.invitee), state)
                self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_only_the_applicants_expired_invitation_is_replaced(self):
        server = ProvisionServer()
        others = [server.invitation(i, student_id=1000+i, expired=True) for i in range(100)]
        server.invites = others + [server.invitation(101, expired=True)]
        with patch.object(core, "api", side_effect=server):
            self.assertEqual(core.ensure_access(123, server.invitee), "pending")
        self.assertEqual([c[:2] for c in server.calls if c[0] != "GET"],
                         [("DELETE", "repositories/123/invitations/101"),
                          ("PUT", "repositories/123/collaborators/Student-123")])
        self.assertEqual(server.invites, others + [server.invitation(42)])

    def test_accepted_or_already_sent_after_lost_invite_response(self):
        for accepted in (True, False):
            server = ProvisionServer()
            def lost(method, path, data=None, **options):
                value = server(method, path, data, **options)
                if method == "PUT":
                    server.can_write = accepted
                    raise core.GitHubError("response lost", temporary=True)
                return value
            with self.subTest(accepted=accepted), patch.object(core, "api", side_effect=lost):
                self.assertEqual(core.ensure_access(123, server.invitee), "write" if accepted else "pending")
            self.assertEqual(sum(method == "PUT" for method, _, _ in server.calls), 1)

    def test_unverifiable_invite_does_not_publish(self):
        server = ProvisionServer()
        def no_effect(method, path, data=None, **options):
            if method == "PUT" and "/collaborators/" in path:
                return None
            return server(method, path, data, **options)
        with patch.object(core, "api", side_effect=no_effect), patch.object(core.time, "sleep"), \
                patch.object(core, "check_configuration", side_effect=server.check):
            with self.assertRaisesRegex(RuntimeError, "Could not verify write access"):
                server.enroll()
        self.assertFalse(server.published)

    def test_reassigned_login_and_wrong_repository_invites_are_not_modified(self):
        server = ProvisionServer()
        with patch.object(core, "api", side_effect=server), self.assertRaisesRegex(ValueError, "expected personal"):
            core.provision("Student-123", "2073", enroll.COURSES["2073"], 222)
        other_repo = dict(server.invitation(33, expired=True), repository={"id": 888})
        speaker = dict(server.invitee, id=222)
        with patch.object(core, "api", side_effect=server), self.assertRaisesRegex(ValueError, "different user"):
            core.ensure_access(123, speaker)
        server.invites = [other_repo]
        with patch.object(core, "api", side_effect=server), self.assertRaisesRegex(ValueError, "another repository"):
            core.ensure_access(123, server.invitee)
        self.assertTrue(all(method == "GET" for method, _, _ in server.calls))

    def test_concurrent_issue_close_is_not_reopened(self):
        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}), patch.object(
                enroll, "provision", return_value=("url", "check", "pending")), patch.object(
                enroll, "api", side_effect=[{}, core.GitHubError("already closed", 422), {"state": "closed"}]) as api:
            enroll.process_application(application(), "https://github.com/run")
        self.assertEqual([c.args[0] for c in api.call_args_list], ["POST", "PATCH", "GET"])
        self.assertIn("有效写入邀请", api.call_args_list[0].args[2]["body"])


if __name__ == "__main__":
    unittest.main()
