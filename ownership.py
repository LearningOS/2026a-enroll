"""Bind a student repository to the applicant's numeric GitHub ID."""

import hmac
import os
import re
from urllib.parse import urlencode
import github_api as github

HUB = "LearningOS/2026a-enroll"
OWNER_PROPERTY = "opencamp_2026a_student_id"


def preparation_description(repository, applicant_id):
    # Authenticate interrupted creates before the org-only ID property exists.
    # Never trust a repository's public description as a claim on its own.
    message = f"2026a-enroll:{repository.lower()}:{applicant_id}".encode()
    signature = hmac.digest(os.environ["GH_TOKEN"].encode(), message, "sha256").hex()
    return f"OpenCamp 2026a preparation for GitHub user {applicant_id} ({signature})"


def receipt_matches(issue, repo, student):
    repo_url = "https://github.com/" + repo["full_name"]
    run_link = re.compile(r"\[本次配置检查已通过\]\(" + re.escape(repo_url) + r"/actions/runs/(\d+)\)", re.I)
    if issue.get("pull_request") or issue["user"]["id"] != student["id"]:
        return False
    for comment in github.pages(f"repos/{HUB}/issues/{issue['number']}/comments"):
        if comment["user"]["id"] != 41898282 or comment["user"]["login"] != "github-actions[bot]":
            continue
        match = run_link.search(comment["body"])
        if match:
            run = github.api("GET", f"repositories/{repo['id']}/actions/runs/{match[1]}", missing_ok=True)
            if (run and run["repository"]["id"] == repo["id"] and run["event"] == "workflow_dispatch"
                    and run["path"] == ".github/workflows/check-config.yml" and run["conclusion"] == "success"):
                return True
    return False


def legacy_receipt(repo, student, issue_number):
    if issue_number:
        issue = github.api("GET", f"repos/{HUB}/issues/{issue_number}")
        if receipt_matches(issue, repo, student):
            return True
    query = urlencode({"q": f"repo:{HUB} is:issue author:{student['login']}"})
    for issue in github.pages("search/issues?" + query, key="items"):
        if issue["number"] != issue_number and receipt_matches(issue, repo, student):
            return True
    return False


def recorded_id(path):
    for prop in github.api("GET", path):
        if prop["property_name"] == OWNER_PROPERTY:
            return prop["value"]
    return None


def bind_owner(repo, student, preparing, issue_number):
    path = f"repositories/{repo['id']}/properties/values"
    expected = str(student["id"])
    bound = recorded_id(path)
    if bound is None:
        # The generator sets this description before any student has access.
        marked = preparing and repo.get("description") == preparation_description(repo["full_name"], student["id"])
        if not marked and not legacy_receipt(repo, student, issue_number):
            raise ValueError("Cannot verify this legacy repository against the original applicant's GitHub ID.")
        github.api("PATCH", path, {"properties": [{"property_name": OWNER_PROPERTY, "value": expected}]})
        bound = recorded_id(path)
    if bound != expected:
        raise ValueError("Repository belongs to another GitHub user ID or the binding is not yet visible.")
