"""Create a course repository for the author of a GitHub Issue."""

import json
import os
from pathlib import Path
import re
import sys

from github_api import GitHubError, api, redact
from provision import ConfigurationError, provision

ROOT = Path(__file__).resolve().parent
ORGANIZATION = "LearningOS"
HUB = ORGANIZATION + "/2026a-enroll"
COURSES = json.loads((ROOT / "courses.json").read_text())


class EnrollmentClosed(ValueError):
    """A recognized course is not accepting new assignment repositories."""


class Withdrawn(Exception):
    """The applicant closed an automatic request while it was queued or running."""


def parse_request(issue):
    if issue.get("pull_request") is not None:
        raise ValueError("Pull requests are not enrollment applications.")
    user = issue["user"]
    login = user["login"]
    if user.get("type") != "User" or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
        raise ValueError("The applicant must be a personal GitHub account.")
    # Parse only the form's fixed course field. Never use a supplied account,
    # template, repository path or command from the issue body.
    matches = re.findall(r"^### 课程\s*\n+([^\n]+)", issue.get("body") or "", re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("请使用“领取作业仓库”申请表，选择一门课程。")
    choice = matches[0].strip()
    for course_id, course in COURSES.items():
        # Keep earlier applications retryable after switching the form to course names.
        if choice in (course["title"], f"{course_id} · {course['title']}"):
            if course.get("enrollment_open") is not True:
                raise EnrollmentClosed(f"{course['title']}暂未开放作业仓库领取，请等待开课通知。")
            return login, course_id, course
    raise ValueError("课程不在本期领取列表中，请重新选择课程。")


def main():
    os.chdir(ROOT)
    (ROOT / "tmp").mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(ROOT / "tmp")
    if os.environ.get("GITHUB_REPOSITORY") != HUB:
        raise ValueError("Run this workflow only in " + HUB)
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if event_name == "issues" and event.get("action") == "opened":
        number = event["issue"]["number"]
    elif event_name == "workflow_dispatch":
        number = os.environ.get("RETRY_ISSUE_NUMBER", "")
        if not re.fullmatch(r"[1-9][0-9]*", number):
            raise ValueError("Enter the numeric application issue number.")
    else:
        raise ValueError("Only new applications or maintainer retries are supported.")

    run_url = f"https://github.com/{HUB}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    path = f"repos/{HUB}/issues/{number}"
    issue = api("GET", path, issue=True)
    automatic = event_name == "issues"
    if automatic and issue["state"] != "open":
        print(f"Skipping closed application #{number}.")
        return
    def check_active():
        if api("GET", path, issue=True)["state"] != "open":
            raise Withdrawn()
    process_application(issue, run_url, check_active if automatic else None)


def process_application(issue, run_url, check_active=None):
    number = issue["number"]
    url = check_url = None
    try:
        login, course_id, course = parse_request(issue)
        if not os.environ.get("GH_TOKEN"):
            raise ValueError("领取入口尚未配置 ENROLL_GITHUB_TOKEN，请维护者完成一次性建仓授权。")
        url, check_url, access = provision(login, course_id, course, issue["user"]["id"], number, check_active)
        if check_active:
            check_active()
        access_step = ("已确认你的账号有写入权限，可以直接进入仓库。" if access == "write" else
                       f"已确认有一份发给你本人的有效写入邀请，请先[接受仓库邀请]({url}/invitations)。")
        body = (
            f"@{login}，你的 **{course['title']}** 作业仓库已配置。\n\n"
            f"1. {access_step}\n"
            f"2. [打开作业仓库]({url})，按 README 克隆、完成实验并 push。\n"
            f"3. 在 [Actions]({url}/actions) 查看评测和成绩上传结果。\n\n"
            "请先加入 [OpenCamp 秋冬季训练营](https://opencamp.cn/os2edu/camp/2026fall)，"
            f"并绑定 GitHub 账号 **{login}**。\n\n"
            f"[本次配置检查已通过]({check_url})，确认身份映射和课程凭证已配置。"
            "配置检查不会提交成绩，实际成绩由之后的实验 push 触发评测上传。"
        )
        api("POST", f"repos/{HUB}/issues/{number}/comments", {"body": body}, issue=True)
        path = f"repos/{HUB}/issues/{number}"
        try:
            api("PATCH", path, {"state": "closed"}, issue=True)
        except GitHubError as error:
            if error.status != 422 or api("GET", path, issue=True)["state"] != "closed":
                raise
    except Withdrawn:
        print(f"Application #{number} was closed during enrollment.")
        return
    except EnrollmentClosed as error:
        body = (str(error) + "\n\n目前仅开放[导学阶段-Rust 语言基础作业领取]"
                f"(https://github.com/{HUB}/issues/new?template=rustlings.yml)。")
        api("POST", f"repos/{HUB}/issues/{number}/comments", {"body": body}, issue=True)
        api("PATCH", f"repos/{HUB}/issues/{number}",
            {"state": "closed", "state_reason": "not_planned"}, issue=True)
        print(str(error), flush=True)
        return
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(redact(str(error)), flush=True)
        message = (f"本次领取未完成。请维护者查看[运行日志]({run_url})，"
                   f"在领取工作流中重试原申请 #{number}。若准备仓已经生成，会接着配置，无需删仓。")
        if url is not None:
            message = (f"[作业仓库]({url})已准备完成，[配置检查]({check_url})已通过。"
                       f"回复或关闭申请时发生错误，请维护者查看[运行日志]({run_url})后重试。")
        if isinstance(error, ConfigurationError):
            message += f"\n\n领取器未能确认[本次配置检查]({error.url})通过。"
        try:
            if check_active:
                check_active()
            api("POST", f"repos/{HUB}/issues/{number}/comments", {"body": message}, issue=True)
        except Withdrawn:
            print(f"Application #{number} was closed during enrollment.")
            return
        except (ValueError, RuntimeError, OSError) as notification_error:
            print("Could not report failure to the issue: " + redact(str(notification_error)), flush=True)
        raise
    print(f"Enrolled {login}: course={course_id}, repository={url}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        sys.exit(redact(str(error)))
