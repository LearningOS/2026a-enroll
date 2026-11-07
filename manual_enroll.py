"""Enroll one student on behalf of an organization owner."""

import os
import sys

from enroll import COURSES, EnrollmentClosed
from github_api import api, redact
from provision import ORGANIZATION, provision


def require_owner(login):
    member = api("GET", f"orgs/{ORGANIZATION}/memberships/{login}")
    if member["state"] != "active" or member["role"] != "admin":
        raise ValueError(f"{login} must be an owner of {ORGANIZATION}.")
    return member["user"]["id"]


def main():
    actor = os.environ["GITHUB_ACTOR"]
    if require_owner(actor) != int(os.environ["GITHUB_ACTOR_ID"]):
        raise ValueError("The dispatcher's GitHub identity has changed.")
    retry_actor = os.environ["GITHUB_TRIGGERING_ACTOR"]
    if retry_actor != actor:
        require_owner(retry_actor)

    course_id = os.environ["COURSE_ID"]
    course = COURSES[course_id]
    if not course.get("enrollment_open"):
        raise EnrollmentClosed(f"{course['title']}暂未开放领取。")
    student_id = int(os.environ["STUDENT_ID"])
    student = api("GET", f"user/{student_id}")
    url, check_url, access = provision(student["login"], course_id, course, student_id)
    print("Repository ready:", url)
    print("Configuration passed:", check_url)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"[作业仓库]({url}) · [配置检查]({check_url}) · {access}\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        sys.exit(redact(str(error)))
