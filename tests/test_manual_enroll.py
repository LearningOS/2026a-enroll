import os
import unittest
from io import StringIO
from unittest.mock import patch

import manual_enroll as manual


class ManualEnrollmentTests(unittest.TestCase):
    def run_manual(self, responses, rerun_by="owner"):
        env = {"GITHUB_ACTOR": "owner", "GITHUB_ACTOR_ID": "10", "GITHUB_TRIGGERING_ACTOR": rerun_by,
               "COURSE_ID": "2078", "STUDENT_ID": "123", "GITHUB_STEP_SUMMARY": "summary"}
        with patch.dict(os.environ, env), patch.object(manual, "api", side_effect=responses), patch(
                "builtins.open", return_value=StringIO()):
            manual.main()

    def test_owner_dispatch_passes_student_id_to_shared_provisioner(self):
        owner = {"state": "active", "role": "admin", "user": {"id": 10}}
        with patch.object(manual, "provision", return_value=("repo", "check", "pending")) as provision:
            self.run_manual([owner, {"login": "Student", "id": 123}])
        provision.assert_called_once_with("Student", "2078", manual.COURSES["2078"], 123)

    def test_unauthorized_dispatch_or_rerun_cannot_provision(self):
        owner = {"state": "active", "role": "admin", "user": {"id": 10}}
        member = {"state": "active", "role": "member", "user": {"id": 20}}
        for replies, actor in (([member], "owner"), ([owner, member], "other"),
                               ([dict(owner, user={"id": 99})], "owner")):
            with self.subTest(actor=actor), patch.object(manual, "provision") as provision:
                with self.assertRaises(ValueError):
                    self.run_manual(replies, actor)
                provision.assert_not_called()
