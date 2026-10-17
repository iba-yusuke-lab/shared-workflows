"""runner_switch は、Blacksmith の今月の使用量を数え、org のリポジトリの変数 RUNNER を切り替える。

各リポジトリのワークフローは `runs-on: ${{ vars.RUNNER || 'blacksmith-2vcpu-ubuntu-2404' }}` で、RUNNER がなければ Blacksmith で走る。
Blacksmith は無料枠を超えても止まらずに課金を続けるので、しきい値を超えたら RUNNER を GitHub のランナーにし、月が替わったら消して Blacksmith に戻す。
GitHub のランナーは org に支払い方法がないので、枠が尽きたら止まり、課金は起きない。

標準ライブラリだけで書いているのは、ランナーに何も入れずに走らせ、依存の更新を追わずに済ませるため。
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UTC = dt.timezone.utc

# GitHub の API は、created で絞った一覧を最大 1,000 件しか返さない（それより後ろのページは空になる）。
# ref: https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository
SEARCH_CAP = 1000

# 締めた区間に入れるのは、作られてから 48 時間たった run だけにする。
# 48 時間にしているのは、どのワークフローの timeout-minutes よりも十分に長く、締めたあとに run の中のジョブが増えないため（再実行で増える分は README の「数えない分」にある）。
SETTLE_AFTER = dt.timedelta(hours=48)


@dataclasses.dataclass
class Config:
    """Config は、環境変数から読む bot の設定を持つ。"""

    token: str
    org: str
    state_repo: str
    threshold_minutes: int
    free_minutes: int
    fallback_runner: str
    blacksmith_label_prefix: str
    revert_not_before_hours: int
    dry_run: bool
    repos: list[str]
    issue_repo: str

    @staticmethod
    def from_env() -> Config:
        """from_env は、ワークフローが渡す環境変数から設定を組み立てる。"""
        org = os.environ["ORG"]
        # MANAGED_REPOS はリポジトリ名のカンマ区切りで、トークンの repositories と同じ値を受ける。
        repos = [f"{org}/{name.strip()}" for name in os.environ["MANAGED_REPOS"].split(",") if name.strip()]
        if not repos:
            raise RuntimeError("MANAGED_REPOS が空")
        return Config(
            token=os.environ["GH_TOKEN"],
            org=os.environ["ORG"],
            state_repo=os.environ["STATE_REPO"],
            threshold_minutes=int(os.environ["THRESHOLD_MINUTES"]),
            free_minutes=int(os.environ["FREE_MINUTES"]),
            fallback_runner=os.environ["FALLBACK_RUNNER"],
            blacksmith_label_prefix=os.environ["BLACKSMITH_LABEL_PREFIX"],
            revert_not_before_hours=int(os.environ["REVERT_NOT_BEFORE_HOURS"]),
            dry_run=os.environ.get("DRY_RUN", "true") == "true",
            repos=repos,
            issue_repo=f"{org}/{os.environ['ISSUE_REPO'].strip()}",
        )


class GitHub:
    """GitHub は、REST API を叩く薄い入れ物。"""

    def __init__(self, token: str) -> None:
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        """request は、API を 1 回呼び、ステータスと JSON を返す。"""
        url = path if path.startswith("https://") else API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
                return res.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            # 404 だけを呼び出し側に返すのは、変数がまだないことを 404 で見分けるため（ほかの失敗は止めて、ジョブを落とす）。
            if err.code == 404:
                return 404, None
            raise RuntimeError(f"{method} {url} が {err.code} で失敗した: {err.read().decode(errors='replace')}") from err

    def get(self, path: str) -> dict:
        """get は、GET して 404 なら止める。"""
        status, body = self.request("GET", path)
        if status == 404 or body is None:
            raise RuntimeError(f"GET {path} が見つからない")
        return body


def iso(t: dt.datetime) -> str:
    """iso は、日時を GitHub の API が受け取る UTC の ISO 8601 にする。"""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> dt.datetime:
    """parse は、GitHub の API が返す日時を読む。"""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def period_start(now: dt.datetime) -> dt.datetime:
    """period_start は、Blacksmith の無料枠が戻る月初（UTC の 1 日 0 時）を返す。"""
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def list_runs(gh: GitHub, repo: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """list_runs は、start から end まで（両端を含む）に作られた run を全部返す。"""
    if end < start:
        return []
    query = urllib.parse.quote(f"{iso(start)}..{iso(end)}")
    first = gh.get(f"/repos/{repo}/actions/runs?per_page=100&created={query}")
    # 1,000 件を超える区間は後ろが取れないので、半分に割って取り直す。
    if first["total_count"] > SEARCH_CAP:
        mid = start + (end - start) / 2
        return list_runs(gh, repo, start, mid) + list_runs(gh, repo, mid + dt.timedelta(seconds=1), end)
    runs = list(first["workflow_runs"])
    page = 2
    while len(runs) < first["total_count"]:
        body = gh.get(f"/repos/{repo}/actions/runs?per_page=100&created={query}&page={page}")
        if not body["workflow_runs"]:
            break
        runs += body["workflow_runs"]
        page += 1
    return runs


def blacksmith_minutes(gh: GitHub, cfg: Config, repo: str, run_id: int, since: dt.datetime, now: dt.datetime) -> int:
    """blacksmith_minutes は、1 つの run のうち、since 以降に Blacksmith で始まったジョブの分数を足す。"""
    total = 0
    page = 1
    while True:
        # filter=all にしているのは、再実行した回のジョブも課金されるため（既定の latest は最後の回しか返さない）。
        body = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}")
        for job in body["jobs"]:
            if not any(label.startswith(cfg.blacksmith_label_prefix) for label in job.get("labels") or []):
                continue
            if not job.get("started_at"):
                continue
            started = parse(job["started_at"])
            if started < since:
                continue
            # 走っている途中のジョブは今の時刻までを数える（終わってから数えると、その間に使った分を見落とす）。
            ended = parse(job["completed_at"]) if job.get("completed_at") else now
            seconds = (ended - started).total_seconds()
            # 分未満を切り上げるのは、Blacksmith がジョブごとに分未満を切り上げて数えるため。
            # この数え方は、Blacksmith のダッシュボードの値とほぼ一致し、わずかに多めに出る（安全側にずれる）。
            if seconds > 0:
                total += math.ceil(seconds / 60)
        if len(body["jobs"]) < 100:
            return total
        page += 1


def count(gh: GitHub, cfg: Config, repos: list[str], start: dt.datetime, end: dt.datetime, since: dt.datetime, now: dt.datetime) -> int:
    """count は、repos で start から end までに作られた run の、Blacksmith の分数を足す。"""
    total = 0
    for repo in repos:
        # 区間を割ったときの境目で同じ run が 2 回出ないように、id で 1 回だけ数える。
        run_ids = sorted({run["id"] for run in list_runs(gh, repo, start, end)})
        # 並べて呼ぶのは、初回や月初は数千の run のジョブを引くことになり、1 つずつだとジョブの timeout-minutes に届くため。
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            total += sum(pool.map(lambda run_id: blacksmith_minutes(gh, cfg, repo, run_id, since, now), run_ids))
    return total


def get_variable(gh: GitHub, repo: str, name: str) -> str | None:
    """get_variable は、リポジトリの変数を読み、なければ None を返す。"""
    status, body = gh.request("GET", f"/repos/{repo}/actions/variables/{name}")
    return None if status == 404 or body is None else body["value"]


def set_variable(gh: GitHub, repo: str, name: str, value: str) -> None:
    """set_variable は、リポジトリの変数を作るか書き換える。"""
    if get_variable(gh, repo, name) is None:
        gh.request("POST", f"/repos/{repo}/actions/variables", {"name": name, "value": value})
    else:
        gh.request("PATCH", f"/repos/{repo}/actions/variables/{name}", {"name": name, "value": value})


def delete_variable(gh: GitHub, repo: str, name: str) -> None:
    """delete_variable は、リポジトリの変数を消す（なければ何もしない）。"""
    gh.request("DELETE", f"/repos/{repo}/actions/variables/{name}")


def load_state(gh: GitHub, cfg: Config, start: dt.datetime) -> dict:
    """load_state は、前回までに締めた分数を読み、月が替わっていたら空から始める。"""
    raw = get_variable(gh, cfg.state_repo, "RUNNER_SWITCH_STATE")
    state = json.loads(raw) if raw else {}
    if state.get("period") != iso(start):
        # 締めの区間を月初の 1 日前から始めるのは、前月の末に作られて月初に走った run のジョブも数えるため（ジョブは始まった時刻で今月かどうかを決める）。
        return {"period": iso(start), "settled_until": iso(start - dt.timedelta(days=1)), "settled_minutes": 0, "issue": state.get("issue")}
    return state


def decide(cfg: Config, used: int, now: dt.datetime, start: dt.datetime, current: str | None) -> str | None:
    """decide は、今月の分数から RUNNER のあるべき値を決める（None は変数を消して Blacksmith に戻すこと）。"""
    if used >= cfg.threshold_minutes:
        return cfg.fallback_runner
    # 月初から数時間は GitHub のランナーのまま待つのは、Blacksmith の月の切り替わりが UTC の 0 時かどうか確かめておらず、先に戻すと前月の枠を超えた分として課金されうるため。
    if current is not None and now < start + dt.timedelta(hours=cfg.revert_not_before_hours):
        return current
    return None


def notify(gh: GitHub, cfg: Config, state: dict, desired: str | None, used: int, now: dt.datetime) -> None:
    """notify は、GitHub のランナーへ替えたときに Issue を立て、Blacksmith に戻したときに閉じる。"""
    issue = state.get("issue")
    # 古い形（番号だけ）は、Issue をこのリポジトリに立てていたころの state なので、このリポジトリの Issue として扱う。
    if isinstance(issue, int):
        issue = {"repo": cfg.state_repo, "number": issue}
    if desired is not None and issue is None:
        _, body = gh.request("POST", f"/repos/{cfg.issue_repo}/issues", {
            "title": f"Blacksmith の使用量が {cfg.threshold_minutes} 分を超えたので、GitHub のランナーに切り替えた",
            "body": "\n\n".join([
                f"{iso(now)} の時点で、今月の Blacksmith の使用量は {used} 分（無料枠 {cfg.free_minutes} 分）だった。",
                f"Runner Switch の bot が、見張るリポジトリの変数 `RUNNER` を `{desired}` にしたので、次のジョブから GitHub のランナーで走る。",
                "月が替わると bot が `RUNNER` を消して Blacksmith に戻し、この Issue を閉じる。",
            ]),
        })
        issue = {"repo": cfg.issue_repo, "number": body["number"]} if body else None
    elif desired is None and issue is not None:
        gh.request("POST", f"/repos/{issue['repo']}/issues/{issue['number']}/comments", {
            "body": f"{iso(now)} に月が替わったので、bot が `RUNNER` を消して Blacksmith に戻した。",
        })
        gh.request("PATCH", f"/repos/{issue['repo']}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
        issue = None
    state["issue"] = issue


def mask(values: list[str]) -> None:
    """mask は、ログに出たら伏せる値を Actions に知らせる。"""
    # このリポジトリは public で Actions のログも外から見えるので、private のリポジトリの名前が例外のメッセージなどに混ざっても伏せる。
    # ref: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#masking-a-value-in-a-log
    for value in values:
        print(f"::add-mask::{value}")


def main() -> int:
    """main は、数えて決めて、リポジトリの変数と Issue に反映する。"""
    cfg = Config.from_env()
    mask(sorted({part for repo in [*cfg.repos, cfg.issue_repo] for part in (repo, repo.split("/", 1)[1])}))
    gh = GitHub(cfg.token)
    now = dt.datetime.now(UTC)
    start = period_start(now)
    repos = cfg.repos
    state = load_state(gh, cfg, start)

    # 48 時間より前の run は締めて state に足し、次からは数え直さない。
    # 毎回月初から数え直すと、月末には 1 回で数千回 API を呼び、GitHub App のトークンの上限（1 時間 5,000 回）に届くため。
    settled_until = parse(state["settled_until"])
    settle_to = max(settled_until, now - SETTLE_AFTER)
    newly_settled = count(gh, cfg, repos, settled_until + dt.timedelta(seconds=1), settle_to, start, now)
    recent = count(gh, cfg, repos, settle_to + dt.timedelta(seconds=1), now, start, now)
    used = state["settled_minutes"] + newly_settled + recent

    current = get_variable(gh, repos[0], "RUNNER")
    desired = decide(cfg, used, now, start, current)

    # 結果をログではなく変数に書くのは、このリポジトリの Actions のログが外から見えるため（使用量も切り替えの有無も出さない）。
    result = {"checked_at": iso(now), "used_minutes": used, "threshold_minutes": cfg.threshold_minutes, "runner": desired}
    if cfg.dry_run:
        set_variable(gh, cfg.state_repo, "RUNNER_SWITCH_DRY_RUN", json.dumps(result))
        print("dry-run の結果を、このリポジトリの変数 RUNNER_SWITCH_DRY_RUN に書いた")
        return 0

    for repo in repos:
        # 全リポジトリを毎回あるべき値にそろえるのは、人が手で変えた値や、途中で落ちた回の書き残しも次の回で直すため。
        if get_variable(gh, repo, "RUNNER") != desired:
            if desired is None:
                delete_variable(gh, repo, "RUNNER")
            else:
                set_variable(gh, repo, "RUNNER", desired)
        # 各リポジトリの CI がこの時刻を見て、bot が止まっていたら警告を出す。
        set_variable(gh, repo, "RUNNER_CHECKED_AT", iso(now))
        set_variable(gh, repo, "RUNNER_BLACKSMITH_MINUTES", str(used))

    notify(gh, cfg, state, desired, used, now)
    state["settled_until"] = iso(settle_to)
    state["settled_minutes"] += newly_settled
    set_variable(gh, cfg.state_repo, "RUNNER_SWITCH_STATE", json.dumps(state))
    print("確認を終えた（今月の分数は、見張るリポジトリの変数 RUNNER_BLACKSMITH_MINUTES にある）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
