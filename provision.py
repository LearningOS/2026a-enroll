"""Prepare course repositories and publish their formal names after verified CI."""

import time
from github_api import GitHubError, REQUEST_BUDGET, REQUEST_TIMEOUT, api

ORGANIZATION = "LearningOS"
CONFIGURATION_TIMEOUT = 600
POLL_INTERVAL = 10
VISIBILITY_DELAYS = (0, 2, 4, 8, 16, 32)


class ConfigurationError(RuntimeError):
    def __init__(self, message, url):
        super().__init__(f"{message}: {url}")
        self.url = url


def check_configuration(repository):
    endpoint = "repos/" + repository
    # Dispatch metadata identifies this exact run, even if other runs exist.
    dispatched = api("POST", endpoint + "/actions/workflows/check-config.yml/dispatches",
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
        run = api("GET", attempt, missing_ok=True)
        status = run["status"] if run is not None else "not yet visible"
        if run is not None and run["status"] == "completed" and run.get("conclusion") is not None:
            if run["conclusion"] != "success":
                raise ConfigurationError(f"Configuration check ended with {run['conclusion']}", url)
            jobs = api("GET", attempt + "/jobs", missing_ok=True)
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
            return api(method, path)
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


def prepare_repository(repository, template, course, login, course_id):
    endpoint = "repos/" + repository
    repo = api("GET", endpoint, missing_ok=True)
    if repo is not None:
        validate_source(repo, template)
        print(f"Resuming preparation: {repository} (id={repo['id']})", flush=True)
    else:
        try:
            repo = api("POST", "repos/" + template + "/generate", {
                "owner": ORGANIZATION, "name": repository.split("/", 1)[1],
                "private": False, "include_all_branches": True,
                "description": f"{course['title']} - {login}",
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
        repo = api("GET", endpoint, missing_ok=True)
        if repo and (repo["id"] != expected_id or repo["full_name"].lower() != repository.lower()):
            raise ValueError("Preparation repository identity changed; left untouched.")
        branches = api("GET", endpoint + "/branches?per_page=100", missing_ok=True) if repo else None
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
        existing = api("GET", "repos/" + final_repository, missing_ok=True)
        if existing is not None:
            if existing["id"] != repository_id:
                raise ValueError("The final repository name is already occupied; nothing was overwritten.")
            return
        current = api("GET", "repos/" + preparing)
        if current["id"] != repository_id:
            raise ValueError("Preparation repository identity changed; nothing was renamed.")
        try:
            api("PATCH", "repos/" + preparing, {
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
            existing = api("GET", "repos/" + final_repository, missing_ok=True)
            if existing is not None and existing["id"] == repository_id:
                return
            if attempt == 4:
                raise
        else:
            existing = api("GET", "repos/" + final_repository, missing_ok=True)
            if existing is not None and existing["id"] == repository_id:
                return
            if attempt == 4:
                raise RuntimeError("Cannot verify the published repository ID.")
        if time.monotonic() + delay + REQUEST_TIMEOUT > deadline:
            raise RuntimeError(f"Rename requires waiting {delay}s; no early retry was sent. Retry the application later.")
        time.sleep(delay)


def student_access(endpoint, student):
    """Return write access, a matching invitation, or neither. Never act on a login alone."""
    permission = api("GET", endpoint + "/collaborators/" + student["login"] + "/permission")
    user = permission["user"]
    if user["id"] != student["id"]:
        raise ValueError("GitHub login now identifies a different user; access was not changed.")
    if user.get("permissions", {}).get("push") or permission["permission"] in {"write", "maintain", "admin"}:
        return "write", None
    page = 1
    expired = []
    while True:
        invitations = api("GET", endpoint + f"/invitations?per_page=100&page={page}")
        for invite in invitations:
            if (invite.get("invitee") or {}).get("id") != student["id"]:
                continue
            if endpoint != f"repositories/{invite['repository']['id']}":
                raise ValueError("Invitation belongs to another repository; left untouched.")
            if invite.get("expired") is True:
                expired.append(invite["id"])
                continue
            if invite.get("expired") is False and invite["permissions"] in {"write", "maintain", "admin"}:
                return "pending", invite["id"]
            raise ValueError("The applicant has an invitation with unverified expiry or insufficient access; "
                             "left untouched for maintainer review.")
        if len(invitations) < 100:
            if len(expired) > 1:
                raise ValueError("Multiple expired invitations for this applicant; left untouched.")
            if expired:
                return "expired", expired[0]
            return "missing", None
        page += 1


def verify_access(endpoint, student):
    for delay in VISIBILITY_DELAYS:
        if delay:
            time.sleep(delay)
        state, _ = student_access(endpoint, student)
        if state in {"write", "pending"}:
            return state
    raise RuntimeError("Could not verify write access or an unexpired write invitation for the applicant. "
                       "Retry the application; the repository was preserved.")


def ensure_access(repository_id, student):
    endpoint = f"repositories/{repository_id}"
    state, invitation_id = student_access(endpoint, student)
    if state in {"write", "pending"}:
        return state
    if state == "expired":
        api("DELETE", endpoint + f"/invitations/{invitation_id}", missing_ok=True)
        for delay in VISIBILITY_DELAYS:
            if delay:
                time.sleep(delay)
            state, _ = student_access(endpoint, student)
            if state in {"write", "pending"}:
                return state
            if state == "missing":
                break
        else:
            raise RuntimeError("Expired invitation is still visible after cancellation; retry this application.")
    try:
        api("PUT", endpoint + "/collaborators/" + student["login"], {"permission": "push"}, retry=False)
    except GitHubError as error:
        if error.status in {403, 429} or not (error.temporary or error.status == 422):
            raise
        # A lost response (or a concurrent acceptance) may already have applied the grant.
        print(str(error), flush=True)
    return verify_access(endpoint, student)


def provision(login, course_id, course, applicant_id):
    # Finish all checks that do not need a repository before creating one.
    student = api("GET", "users/" + login)
    if (student["type"] != "User" or student["login"].lower() != login.lower()
            or student["id"] != applicant_id):
        raise ValueError("The applicant is not the expected personal GitHub account.")
    template = ORGANIZATION + "/" + course["template"]
    source = api("GET", "repos/" + template)
    if not source.get("is_template") or source.get("private") or source.get("archived") or source.get("disabled"):
        raise ValueError(template + " must be a public template repository.")
    branches = api("GET", "repos/" + template + "/branches?per_page=100")
    if not set(course["branches"]).issubset({item["name"] for item in branches}):
        raise ValueError("Course template is missing required branches; no repository was created.")
    secret = api("GET", f"orgs/{ORGANIZATION}/actions/secrets/{course['secret']}")
    if secret.get("visibility") not in {"all", "selected"}:
        raise ValueError("The course organization secret must allow public repositories.")

    final_repository = template + "-" + login
    repo = api("GET", "repos/" + final_repository, missing_ok=True)
    if repo is None:
        repository = ORGANIZATION + "/preparing-" + course["template"] + "-" + login
        repo = prepare_repository(repository, template, course, login, course_id)
    else:
        validate_source(repo, template)
        if repo["full_name"].lower() != final_repository.lower():
            raise ValueError("The final name redirects to another repository; left untouched.")
        repository = final_repository
        print("Checking existing course repository; student code is preserved: " + repository)
    endpoint = "repos/" + repository

    variable_path = endpoint + "/actions/variables/STUDENT_GITHUB"
    variable = api("GET", variable_path, missing_ok=True)
    if variable is None:
        try:
            api("POST", endpoint + "/actions/variables", {"name": "STUDENT_GITHUB", "value": login})
        except GitHubError as error:
            if not error.temporary and error.status != 422:
                raise
            print(str(error), flush=True)
            if error.status in {403, 429}:
                raise
        variable = wait_for_visibility("GET", variable_path)
    if variable["value"].lower() != login.lower():
        raise ValueError("Repository belongs to another student; identity was not overwritten.")

    # Grant only this course credential before checking the student repository.
    if secret["visibility"] == "selected":
        api("PUT", f"orgs/{ORGANIZATION}/actions/secrets/{course['secret']}/repositories/{repo['id']}")

    wait_for_visibility("PUT", endpoint + "/actions/workflows/check-config.yml/enable")
    check_url = check_configuration(repository)
    wait_for_visibility("PUT", endpoint + "/actions/workflows/build.yml/enable")
    ensure_access(repo["id"], student)
    if repository != final_repository:
        publish_repository(repository, final_repository, repo["id"])
        check_url = check_url.replace(repository, final_repository, 1)
        print("Preparation passed; formal repository published: " + final_repository, flush=True)
    formal = api("GET", "repos/" + final_repository)
    if formal["id"] != repo["id"] or formal["full_name"].lower() != final_repository.lower():
        raise ValueError("The formal name no longer identifies the verified repository.")
    access = verify_access(f"repositories/{repo['id']}", student)
    return "https://github.com/" + final_repository, check_url, access

