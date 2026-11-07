"""Prepare course repositories and publish their formal names after verified CI."""

import time
import github_api as github
from access import ensure_access, verify_access
from ownership import bind_owner, preparation_description
from github_api import GitHubError, REQUEST_BUDGET, REQUEST_TIMEOUT, VISIBILITY_DELAYS

ORGANIZATION = "LearningOS"
CONFIGURATION_TIMEOUT = 600
POLL_INTERVAL = 10


class ConfigurationError(RuntimeError):
    def __init__(self, message, url):
        super().__init__(f"{message}: {url}")
        self.url = url


def check_configuration(repository):
    endpoint = "repos/" + repository
    # Dispatch metadata identifies this exact run, even if other runs exist.
    dispatched = github.api("POST", endpoint + "/actions/workflows/check-config.yml/dispatches",
                     {"ref": "main", "return_run_details": True})
    run_id = (dispatched or {}).get("workflow_run_id")
    if type(run_id) is not int or run_id <= 0:
        raise RuntimeError("GitHub did not return a configuration run ID; enrollment was not confirmed.")
    url = f"https://github.com/{repository}/actions/runs/{run_id}"
    # Dispatch starts attempt 1. A later manual re-run must not certify it.
    attempt = endpoint + f"/actions/runs/{run_id}/attempts/1"
    deadline = time.monotonic() + CONFIGURATION_TIMEOUT
    status = "not yet visible"
    while time.monotonic() < deadline:
        run = github.api("GET", attempt, missing_ok=True)
        status = run["status"] if run is not None else "not yet visible"
        if run is not None and run["status"] == "completed" and run.get("conclusion") is not None:
            if run["conclusion"] != "success":
                raise ConfigurationError(f"Configuration check ended with {run['conclusion']}", url)
            jobs = github.api("GET", attempt + "/jobs", missing_ok=True)
            for job in (jobs or {}).get("jobs", []):
                if job["name"] == "configuration" and job.get("conclusion") is not None:
                    if job["conclusion"] != "success":
                        raise ConfigurationError(f"Configuration job ended with {job['conclusion']}", url)
                    print("Configuration check passed: " + url, flush=True)
                    return url
            status = "waiting for the configuration job result on attempt 1"
        print(f"Waiting for configuration check ({status}): {url}", flush=True)
        time.sleep(POLL_INTERVAL)
    raise ConfigurationError(f"Configuration check was not verified within 10 minutes ({status})", url)


def wait_for_visibility(method, path):
    """Only retry 404 for a resource we have just created or expect from the template."""
    for delay in VISIBILITY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return github.api(method, path)
        except GitHubError as error:
            if error.status != 404 or delay == VISIBILITY_DELAYS[-1]:
                raise
            print(f"Waiting for GitHub to expose {path} (404)", flush=True)


def validate_source(repo, template):
    if repo.get("archived") or repo.get("disabled"):
        raise ValueError("Repository is archived or disabled; left untouched.")
    source_name = (repo.get("template_repository") or {}).get("full_name", "")
    if source_name.lower() != template.lower() or repo.get("private"):
        raise ValueError(repo.get("full_name", "Repository") +
                         " already exists with a different source; left untouched.")


def prepare_repository(repository, template, course, applicant_id):
    endpoint = "repos/" + repository
    repo = github.api("GET", endpoint, missing_ok=True)
    if repo is not None:
        validate_source(repo, template)
        print(f"Resuming preparation: {repository} (id={repo['id']})", flush=True)
    else:
        try:
            repo = github.api("POST", "repos/" + template + "/generate", {
                "owner": ORGANIZATION, "name": repository.split("/", 1)[1],
                "private": False, "include_all_branches": True,
                "description": preparation_description(repository, applicant_id),
            })
        except GitHubError as error:
            if not error.temporary and error.status != 422:
                raise
            print(str(error), flush=True)
            if error.status in {403, 429}:
                raise
            repo = wait_for_visibility("GET", endpoint)
            validate_source(repo, template)
            print("Preparation repository exists after create error; continuing configuration.")

    expected_id = repo["id"]
    for attempt in range(30):
        repo = github.api("GET", endpoint, missing_ok=True)
        if repo and (repo["id"] != expected_id or repo["full_name"].lower() != repository.lower()):
            raise ValueError("Preparation repository identity changed; left untouched.")
        branches = github.api("GET", endpoint + "/branches?per_page=100", missing_ok=True) if repo else None
        missing = set(course["branches"]) - {item["name"] for item in branches or []}
        if repo and not missing:
            validate_source(repo, template)
            return repo
        if attempt == 29:
            raise ValueError(f"{repository}: missing branches {sorted(missing)}; "
                             "retry this application to resume the same preparation repository.")
        time.sleep(2)


def publish_repository(preparing, final_repository, repository_id):
    # Rename is the final configuration write. All setup and the exact CI
    # check have passed, and repository invitations follow the repository ID.
    deadline = time.monotonic() + REQUEST_BUDGET
    for attempt in range(1, 5):
        delay = 2 ** attempt
        existing = github.api("GET", "repos/" + final_repository, missing_ok=True)
        if existing is not None:
            if existing["id"] != repository_id:
                raise ValueError("The final repository name is already occupied; nothing was overwritten.")
            return
        current = github.api("GET", "repos/" + preparing)
        if current["id"] != repository_id:
            raise ValueError("Preparation repository identity changed; nothing was renamed.")
        try:
            github.api("PATCH", "repos/" + preparing, {
                "name": final_repository.split("/", 1)[1],
                "description": "OpenCamp coursework; enrollment configuration verified",
            }, retry=False)
        except GitHubError as error:
            # A lost rename response can still mean success. Check the exact
            # repository ID before retrying, never another student's repo.
            if not error.temporary and error.status not in {404, 422}:
                raise
            print(str(error), flush=True)
            delay = max(delay, error.retry_after or 0)
            if error.status in {403, 429} and error.temporary:
                # A rejected rate-limit request did not rename the repository.
                # Do not issue even a reconciliation GET before the allowed wait.
                if attempt == 4:
                    raise
                if time.monotonic() + delay + REQUEST_TIMEOUT > deadline:
                    raise RuntimeError(f"Rename requires waiting {delay}s; no early retry was sent. Retry the application later.")
                time.sleep(delay)
                continue
            existing = github.api("GET", "repos/" + final_repository, missing_ok=True)
            if existing is not None and existing["id"] == repository_id:
                return
            if attempt == 4:
                raise
        else:
            existing = github.api("GET", "repos/" + final_repository, missing_ok=True)
            if existing is not None and existing["id"] == repository_id:
                return
            if attempt == 4:
                raise RuntimeError("Cannot verify the published repository ID.")
        if time.monotonic() + delay + REQUEST_TIMEOUT > deadline:
            raise RuntimeError(f"Rename requires waiting {delay}s; no early retry was sent. Retry the application later.")
        time.sleep(delay)


def verify_branches(repo, course):
    branches = {b["name"] for b in github.pages("repos/" + repo["full_name"] + "/branches")}
    missing = sorted(set(course["branches"]) - branches)
    if missing:
        raise ValueError(f"{repo['full_name']}: missing required branches {missing}.")


def configure_student_login(endpoint, login):
    variable_path = endpoint + "/actions/variables/STUDENT_GITHUB"
    variable = github.api("GET", variable_path, missing_ok=True)
    if variable is None:
        try:
            github.api("POST", endpoint + "/actions/variables", {"name": "STUDENT_GITHUB", "value": login})
        except GitHubError as error:
            if not error.temporary and error.status != 422:
                raise
            print(str(error), flush=True)
            if error.status in {403, 429}:
                raise
        variable = wait_for_visibility("GET", variable_path)
    if variable["value"].lower() != login.lower():
        raise ValueError("Repository belongs to another student; identity was not overwritten.")


def check_course(course):
    template = ORGANIZATION + "/" + course["template"]
    source = github.api("GET", "repos/" + template)
    if not source.get("is_template") or source.get("private") or source.get("archived") or source.get("disabled"):
        raise ValueError(template + " must be a public template repository.")
    branches = github.api("GET", "repos/" + template + "/branches?per_page=100")
    if not set(course["branches"]).issubset({item["name"] for item in branches}):
        raise ValueError("Course template is missing required branches; no repository was created.")
    secret = github.api("GET", f"orgs/{ORGANIZATION}/actions/secrets/{course['secret']}")
    if secret.get("visibility") not in {"all", "selected"}:
        raise ValueError("The course organization secret must allow public repositories.")
    return template, secret


def provision(login, course_id, course, applicant_id, issue_number=None, check_active=None):
    # Finish all checks that do not need a repository before creating one.
    student = github.api("GET", "users/" + login)
    if (student["type"] != "User" or student["login"].lower() != login.lower()
            or student["id"] != applicant_id):
        raise ValueError("The applicant is not the expected personal GitHub account.")
    template, secret = check_course(course)

    final_repository = template + "-" + login
    repo = github.api("GET", "repos/" + final_repository, missing_ok=True)
    if repo is None:
        repository = ORGANIZATION + "/preparing-" + course["template"] + "-" + login
        repo = prepare_repository(repository, template, course, applicant_id)
    else:
        validate_source(repo, template)
        if repo["full_name"].lower() != final_repository.lower():
            raise ValueError("The final name redirects to another repository; left untouched.")
        repository = final_repository
        verify_branches(repo, course)
        print("Checking existing course repository; student code is preserved: " + repository)
    endpoint = "repos/" + repository

    bind_owner(repo, student, repository != final_repository, issue_number)
    configure_student_login(endpoint, login)

    # Grant only this course credential before checking the student repository.
    if secret["visibility"] == "selected":
        github.api("PUT", f"orgs/{ORGANIZATION}/actions/secrets/{course['secret']}/repositories/{repo['id']}")

    wait_for_visibility("PUT", endpoint + "/actions/workflows/check-config.yml/enable")
    check_url = check_configuration(repository)
    wait_for_visibility("PUT", endpoint + "/actions/workflows/build.yml/enable")
    if check_active:
        check_active()
    ensure_access(repo["id"], student)
    if repository != final_repository:
        publish_repository(repository, final_repository, repo["id"])
        check_url = check_url.replace(repository, final_repository, 1)
        print("Preparation passed; formal repository published: " + final_repository, flush=True)
    formal = github.api("GET", "repos/" + final_repository)
    if formal["id"] != repo["id"] or formal["full_name"].lower() != final_repository.lower():
        raise ValueError("The formal name no longer identifies the verified repository.")
    verify_branches(formal, course)
    access = verify_access(f"repositories/{repo['id']}", student)
    return "https://github.com/" + final_repository, check_url, access
