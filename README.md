# shared-workflows

`iba-yusuke-lab` の複数のリポジトリで使う GitHub Actions の仕組みを置く。

| 置いてあるもの | 中身 |
| --- | --- |
| [GitHub Actions CI](#github-actions-ci)（`gha-lint.yaml`） | ワークフローを zizmor・actionlint・ghalint と独自の規約で検査する reusable workflow |
| [Gitleaks](#gitleaks)（`gitleaks.yaml`） | PR で積んだコミットのクレデンシャルを gitleaks で検査する reusable workflow |
| [Runner Switch](#runner-switch)（`runner-switch.yaml`） | Blacksmith の使用量を見て、org のリポジトリのランナーを切り替える bot |

## public のリポジトリに置くための決まり

このリポジトリは public で、コード・履歴・PR・Issue・Actions のログが外から見える。

- **private のリポジトリの名前と、CI の使用量をここに書かない**
  - コード・README・コミット・PR の本文・Issue・Actions のログのどこにも出さない
  - 名前が必要な設定は、このリポジトリの Repository variables に置く（variables の値は外から見えない）
  - Runner Switch は、使用量と結果をログではなく変数に書き、private のリポジトリの名前をログで伏せる
- **ワークフローは GitHub のランナーで走らせる**
  - public のリポジトリでは標準のランナーが無料で、org の無料枠も減らない
  - fork からの PR が `runs-on` を書き換えても、org の Blacksmith で走らないようにする
- **fork からの PR のワークフローは承認してから走らせる**
  - Settings → Actions → General → Fork pull request workflows を「Require approval for all external contributors」にしてある

## reusable workflow の呼び方

呼ぶ側は起動の条件（`on:` と `paths`）だけを持ち、検査の中身はこのリポジトリを SHA で固定して呼ぶ。

```yaml
jobs:
  lint:
    name: GitHub Actions CI
    permissions:
      contents: read # checkout と zizmor の online-audits
    uses: iba-yusuke-lab/shared-workflows/.github/workflows/gha-lint.yaml@<SHA> # v1.0.0
    with:
      runner: ${{ vars.RUNNER || 'blacksmith-2vcpu-ubuntu-2404' }}
```

- `runner` を呼ぶ側から渡すのは、`vars.RUNNER` を呼ぶ側のリポジトリの変数で決めるため
- 呼ぶ側のリポジトリの「使える action」の設定には、ここで使う `zizmorcore/zizmor-action` と `raven-actions/actionlint` も許可しておく
  - 同じ org の reusable workflow は「Allow OWNER」で通るが、その中の action は呼ぶ側の設定で判定される
- 中身を変えたら、タグを打って呼ぶ側の SHA を上げる

## GitHub Actions CI

呼ぶ側の `.github/workflows/` と `.github/actions/` を、zizmor（pedantic）・actionlint・ghalint と独自の規約（`defaults.run.shell: bash` と全ステップの `name`）で検査する。

- lint のバージョンは、呼ぶ側の `mise.toml` に `aqua:zizmorcore/zizmor` などがあればそれを、なければ `gha-lint.yaml` の既定値を使う
- shellcheck の規則は、呼ぶ側の `mise.toml` の `env.SHELLCHECK_OPTS`、`.shellcheckrc` の順に探す
- zizmor・actionlint・ghalint の設定（`.github/zizmor.yml`・`.github/actionlint.yaml`・`.github/ghalint.yaml`）は、それぞれの道具が呼ぶ側のリポジトリから読む

## Gitleaks

PR のベースブランチから HEAD までのコミットを gitleaks で検査する。

- 呼ぶ側の `mise.lock` に gitleaks があれば、その URL とチェックサムで配布物を確かめる
  - なければ `gitleaks.yaml` に固定した URL とチェックサムを使う
  - gitleaks の配布物には attestation がないので、ハッシュを固定して確かめる
- `pull_request` 以外から呼ぶと、検査する範囲が決まらないので落ちる

## Runner Switch

Linux のジョブを Blacksmith と GitHub のランナーのどちらで走らせるかを、Blacksmith の使用量を見て bot が切り替える（`.github/workflows/runner-switch.yaml`、本体は `.github/scripts/runner_switch.py`）。

### なぜ bot に任せるか

Blacksmith は無料枠（月 3,000 分、毎月 1 日に戻る）を超えても止まらず、超えた分を課金し続ける。

GitHub のランナーは、org に支払い方法がないので、無料枠（月 2,000 分）が尽きた時点で止まり、課金は起きない。

そこで、月の前半は Blacksmith で走らせ、使用量がしきい値を超えたら GitHub のランナーに替える。

切り替えを人がメールを見て行うと、見落としたときに課金が続くので、bot に任せる。

しきい値と確認の頻度を決めた経緯は、利用側のリポジトリの ADR にある。

### 仕組み

見張るリポジトリの Linux のジョブは、`runs-on: ${{ vars.RUNNER || 'blacksmith-2vcpu-ubuntu-2404' }}` と書く。

bot は 6 時間ごとに次を行う。

1. 見張るリポジトリの run から、今月 Blacksmith のラベルで始まったジョブを集め、ジョブごとに分未満を切り上げて足す
2. 1,800 分（無料枠の 60%）以上なら、見張るリポジトリの変数 `RUNNER` を `ubuntu-24.04` にし、Issue を立てる
3. 1,800 分未満なら `RUNNER` を消して Blacksmith に戻し、Issue を閉じる
4. 見張るリポジトリに、確認した時刻 `RUNNER_CHECKED_AT` と今月の分数 `RUNNER_BLACKSMITH_MINUTES` を書く

- 月が替わると 3 に当たって Blacksmith に戻る
  - ただし月初から 12 時間は `RUNNER` を消さずに待つ
  - Blacksmith の月の切り替わりが UTC の 0 時かどうかを確かめておらず、先に戻すと前月の枠を超えた分として課金されうるため
- 人が手で `RUNNER` を変えても、次の確認で bot があるべき値に戻す
  - bot を止めて手で切り替えたいときは、Actions の画面でこのワークフローを Disable にする
- 48 時間より前に作られた run は数え終えたものとして、分数を変数 `RUNNER_SWITCH_STATE`（このリポジトリ）に足し、次からは数え直さない
  - 毎回月初から数え直すと、月末には 1 回で数千回 API を呼び、GitHub App のトークンの上限（1 時間 5,000 回）に届くため
  - 中身を消すと、次の回が月初から数え直す（数分かかる）
- bot は GitHub のランナーで走る
  - このリポジトリは public なので、org の GitHub の無料枠を使い切っていても止まらず、月が替わったときに確実に戻せる
- 結果はログに出さない
  - 今月の分数は、見張るリポジトリの変数 `RUNNER_BLACKSMITH_MINUTES` で見る
  - 手で起こした dry-run の結果は、このリポジトリの変数 `RUNNER_SWITCH_DRY_RUN` に書く
  - Issue は、変数 `RUNNER_SWITCH_ISSUE_REPO` が指す private のリポジトリに立てる

### 数えない分

- 48 時間より前に作られた run を再実行したときのジョブ
  - run の作られた時刻で数える区間を決めていて、締めた区間には戻らないため
- `blacksmith-` で始まらないラベルの Blacksmith のランナー
  - 大きいランナーを使い始めたら、`BLACKSMITH_LABEL_PREFIX` と 1 分の重みを見直す

### bot が止まったときの警報

| 警報 | いつ鳴るか |
| --- | --- |
| ワークフローの失敗の通知 | bot が落ちたとき |
| 見張るリポジトリの CI の警告（`RUNNER_CHECKED_AT` が 1 日より古い） | bot が落ちた翌日から。<br>PR の画面で見える |
| Blacksmith の 80% のメール | bot が遅れて 2,400 分を超えたとき |
| Blacksmith の Total spend alert（$1） | 課金が始まったとき |

警報が鳴ったら、このワークフローの実行履歴を見て、落ちていれば直す。

直すまでの間は、見張るリポジトリで `gh variable set RUNNER --body ubuntu-24.04` を走らせ、手で GitHub のランナーに替える。

### 見張るリポジトリを足すとき

1. このリポジトリの変数 `MANAGED_REPOS` に、リポジトリ名をカンマ区切りで足す
2. GitHub App `iba-lab-bot` の install 先に足す
3. Blacksmith の install 先と、org の Settings → Actions → Runner groups に足す
4. そのリポジトリの Linux のジョブの `runs-on` を上の形にし、`RUNNER_CHECKED_AT` の警告を CI に足す

### 必要な設定

すべてこのリポジトリの Settings → Secrets and variables → Actions に置く。

| 名前 | 種類 | 中身 |
| --- | --- | --- |
| `MANAGED_REPOS` | variable | 見張るリポジトリの名前のカンマ区切り（org 名は付けない） |
| `RUNNER_SWITCH_ISSUE_REPO` | variable | 切り替えたときの Issue を立てるリポジトリの名前（private のもの。`MANAGED_REPOS` のどれか） |
| `IBA_LAB_BOT_CLIENT_ID` | variable | GitHub App `iba-lab-bot` の Client ID |
| `IBA_LAB_BOT_PRIVATE_KEY` | secret | このリポジトリ用に発行した `iba-lab-bot` の秘密鍵 |

- GitHub App `iba-lab-bot` には、Actions の read と、Variables と Issues の read and write が必要
- org の Free プランでは、private のリポジトリから org の variables と secrets を読めないので、リポジトリ単位で置く
- 秘密鍵の台帳は、利用側のリポジトリの台帳にある
