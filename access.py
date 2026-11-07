"""Verify student write access and manage invitations on the verified repository ID."""

import time
import github_api as github
from github_api import GitHubError, VISIBILITY_DELAYS


def student_access(endpoint, student):
    """Return write access, a matching invitation, or neither. Never act on a login alone."""
    permission = github.api("GET", endpoint + "/collaborators/" + student["login"] + "/permission")
    user = permission["user"]
    if user["id"] != student["id"]:
        raise ValueError("GitHub login now identifies a different user; access was not changed.")
    if user.get("permissions", {}).get("push") or permission["permission"] in {"write", "maintain", "admin"}:
        return "write", None
    expired = []
    for invite in github.pages(endpoint + "/invitations"):
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

    if len(expired) > 1:
        raise ValueError("Multiple expired invitations for this applicant; left untouched.")
    if expired:
        return "expired", expired[0]
    return "missing", None


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
        github.api("DELETE", endpoint + f"/invitations/{invitation_id}", missing_ok=True)
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
        github.api("PUT", endpoint + "/collaborators/" + student["login"], {"permission": "push"}, retry=False)
    except GitHubError as error:
        if error.status in {403, 429} or not (error.temporary or error.status == 422):
            raise
        # A lost response (or a concurrent acceptance) may already have applied the grant.
        print(str(error), flush=True)
    return verify_access(endpoint, student)
