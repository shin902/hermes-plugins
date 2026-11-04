---
name: secbrief-operations
description: Operate an existing local secbrief checkout for one-shot dependency inventory, OSV/KEV matching, report review, and health status. Use for scan/cycle/report/status requests, coverage and unknown handling, and host-scheduling handoff. Never updates, restarts, or remediates automatically.
version: 0.1.0
metadata:
  hermes:
    tags: [security, vulnerability, secbrief, operations]
    config:
      - key: secbrief.home
        description: Absolute path to the existing secbrief checkout (contains secbrief.py and extras/).
        default: ""
        prompt: Path to the secbrief checkout
---

# secbrief 運用

## このスキルの範囲

既存の secbrief チェックアウトをローカル CLI として操作するための手順。
エンジン、スケジューラ、通知クライアント、ラッパーツールは同梱しない。
このスキル自身は何もスキャンせず、下記のコマンドをエージェントが実行する。

- 単発の棚卸し・照合・レポート確認・状態確認だけを行う。
- 定期実行と通知は Hermes（cron / delivery）が所有する。このプラグインはジョブを作らない。
- 更新、再起動、修復、デプロイは行わない。人間の明示承認なしに実行しない。
- 網羅性や本番運用可能であることを主張しない。

## 1. チェックアウトを解決する

次の順で既存チェックアウトの絶対パスを解決する。推測で clone や install をしない。

1. 環境変数 `SECBRIEF_HOME`
2. プロファイルローカル設定 `skills.config.secbrief.home`
3. ユーザーが明示した既存プロジェクトルート内の読み取り専用探索

```sh
SECBRIEF_HOME="${SECBRIEF_HOME:-}"
if [ -z "$SECBRIEF_HOME" ]; then
  configured="$(hermes config get skills.config.secbrief.home 2>/dev/null | head -n 1 || true)"
  case "$configured" in
    /*) SECBRIEF_HOME="$configured" ;;
  esac
fi
test -f "$SECBRIEF_HOME/secbrief.py" &&
  test -f "$SECBRIEF_HOME/extras/automation.py" &&
  test -f "$SECBRIEF_HOME/extras/secbrief_extra.py" &&
  echo "secbrief: $SECBRIEF_HOME"
```

設定も環境変数も空なら、ユーザーが指定したルートだけで候補を探す。例えば:

```sh
find "$USER_APPROVED_ROOT" -maxdepth 4 -type f -name secbrief.py -print
```

`secbrief.py` と `extras/automation.py` が同じチェックアウトにあり、ユーザーが対象として認めたものだけを使う。見つからない、複数ある、または対象が曖昧なら停止してユーザーに確認する。パスをこのリポジトリやスキル本文へ書き込まない。必要ならプロファイル設定にだけ記録する。

コマンドはチェックアウトをカレントディレクトリにして実行する。

```sh
cd "$SECBRIEF_HOME"
```

## 2. 実際の CLI フロー

secbrief に `scan` や `status` という名前のサブコマンドはない。要求された語を次の実コマンドへ対応付ける。

| 要求 | 実行する実コマンド | ネットワーク |
|---|---|---|
| 棚卸しだけ（scan の前段） | `python3 extras/secbrief_extra.py inventory ...` | なし |
| 依存候補の照合（scan） | `python3 extras/secbrief_extra.py run ...`。オンライン照合は `--online` が明示された場合だけ | `--online` 時のみ外部照合 |
| 従来の core スキャン | `python3 secbrief.py run --config /absolute/path/config.json` | 設定に従い OSV/KEV/Homebrew、設定時は SSH |
| 単発 cycle（scan + 差分判定） | `python3 extras/automation.py cycle --config /absolute/path/operator-config.json` | 設定次第。`online` / `inventory_sources_online` を勝手に有効化しない |
| レポート | `report.json` / `report.md` / `agent-summary.json` を読む。既存証拠の再描画は `render` | `render` はオフライン |
| 状態（status） | `python3 secbrief.py doctor --config ...`、`python3 extras/secbrief_extra.py doctor --output ...`、cycle の JSON `status` | ローカル状態の確認 |

### 棚卸しのみ（ネットワークなし）

```sh
python3 extras/secbrief_extra.py inventory \
  --lockfile /absolute/path/package-lock.json \
  --asar /absolute/path/app.asar \
  --output /absolute/path/secbrief-extra-out
```

複数の `--lockfile` / `--asar` を指定できる。`inventory.json` を生成する。これは候補の記録であり、CVE 照合でもインストール済み製品の証明でもない。エラー時は終了コード 2。

### 単発の照合（scan）

```sh
python3 extras/secbrief_extra.py run \
  --lockfile /absolute/path/package-lock.json \
  --asar /absolute/path/app.asar \
  --output /absolute/path/secbrief-extra-out
```

`--online` を付けた場合だけ、OSV/CISA へパッケージ名・版・エコシステムを送る。付けなければ `inventory_only` であり、脆弱性照合は未完了である。`--online` はユーザーの明示許可を得てから使う。

### core スキャン

```sh
python3 secbrief.py run --config /absolute/path/config.json
```

core は Python 環境、設定された SSH 先 Debian/Ubuntu のパッケージ、Homebrew formula などを棚卸しし、OSV・CISA KEV・formulae.brew.sh を照合する。SSH 先や外部フィードが設定に含まれるため、初回は何が外部へ出るかを説明して許可を得る。`--baseline` は初回のアラートを抑止する用途で、脆弱性が無いことの証明ではない。

### cycle

```sh
python3 extras/automation.py cycle \
  --config /absolute/path/operator-config.json
```

1 回だけ実行し、標準出力に `secbrief-automation/result-v1` の JSON を返す。`status` は次のいずれか。

- `changed`: 新規・変更・修正版公開・棚卸し差分などがある。ユーザーの通知設定に従って表面化する。
- `nochange`: 抑制してよい。ただし「安全」「脆弱性なし」ではない。
- `failed`: 失敗。終了コードは 2。絶対に `nochange` として抑制しない。

`report_status` が `inventory_only` の場合、`nochange` は「棚卸し結果が変わっていない」だけを意味し、CVE 照合済みではない。`degraded`、`unknown`、空の一覧も安全の証拠にしない。

### レポート

通常の cycle / run は次を生成する。

- `report.json`: 機械照合用の完全な証拠。依存、パス、版、finding を含む。
- `report.md`: 短い人間向けレポート。
- `agent-summary.json`: エージェント向けの短い要約。依存一覧・版・パスを含めない。

既存の `report.json` からネットワークなしで再描画する:

```sh
python3 extras/secbrief_extra.py render \
  --report /absolute/path/secbrief-extra-out/report.json \
  --output /absolute/path/secbrief-extra-out/rendered
```

### 状態確認

```sh
python3 secbrief.py doctor --config /absolute/path/config.json
python3 extras/secbrief_extra.py doctor --output /absolute/path/secbrief-extra-out
```

core の `doctor` は最後の成功・degraded・baseline・アラートファイルの更新時刻を表示する。アラートファイルや briefing の mtime が想定周期の 2 倍より古ければ、スケジューラが止まっている可能性として扱う。`extras` の `doctor` は出力ディレクトリ内の `report.json` / `state.json` / `alerts.json` の有無を示す。

## 3. 外部送信とランタイム probe の許可

外部送信は機能ごとに別の許可である。一度の「はい」を将来の定期実行や別機能の包括許可にしない。

- `--online`、`online: true`、`inventory_sources_online`、core の `run` は、パッケージ名・版・エコシステム（および Debian/Ubuntu の source package 識別子）を OSV/CISA 等へ送り得る。実行前に送信内容と宛先を説明し、ユーザーの明示許可を得る。
- `--probe-runtime` と `inventory_sources_probe_runtimes` は別の許可。指定した実行ファイルの版を取得する処理であり、オンライン照合の許可とは独立している。Electron では runAsNode fuse を確認し、許可された場合だけ probe する。通常の静的棚卸しと混同しない。
- 設定ファイルの例示版やパスを実測値として扱わない。対象、周期、通知先、外部送信の許可はユーザーが決める。
- 許可なく `online` / `inventory_sources_online` / `inventory_sources_probe_runtimes` を書き換えない。設定変更後は、どの外部送信が有効になったかを再確認する。

## 4. カバレッジと unknown

### 実際に扱えるもの

- core: Python 環境、設定した SSH 先 Debian/Ubuntu のパッケージ棚卸しと apt simulation、Homebrew formula、OSV / CISA KEV / formulae.brew.sh。
- extras: npm v2/v3 `package-lock.json`、`composer.lock`、Electron の ASAR / unpacked / アプリディレクトリの静的メタデータ。
- プロジェクト発見: 明示ルート以下の Java/Gradle、JS/TS、Python、PHP、Go、Rust、Ruby、.NET の識別ファイル。monorepo の子も記録する。
- ホスト・コンテナ棚卸し: 指定した Node/PHP/Bun、Python 環境、dpkg/pacman/rpm、Docker の一覧。許可した版 probe。
- 一次情報: PHP/Electron/Bun の公式 GHSA、Node 公式 core DB などの補助カタログ。

### 未対応・不明として残すもの

- Java/Maven/Gradle などの解決済み依存グラフ。発見はできても依存解決は未対応。
- 全言語・PC 全体・全プロジェクトの完全な発見。指定ルート、識別ファイル、除外、深さ/件数上限、権限の範囲だけ。
- lockfile はリポジトリ候補であり、インストール済み製品の証拠ではない。アプリの attribution には明示的な ASAR / package メタデータが必要。
- 静的リンクやメタデータを失ったバンドルの完全な版復元、非公開バックポートの確定。
- pacman/rpm の一覧を取得しただけでは CVE 照合完了ではない。Docker 一覧も同様に、中身の完全な照合を意味しない。
- 未知の脆弱性、ゼロデイ、悪用・侵入の有無、全プロセスやネットワーク公開範囲。
- 未対応の版範囲表記、PHP 拡張の有効化条件、未掲載・遅延した情報。推測で確定しない。
- フィードの遅延や欠落。`unknown`、`degraded`、`inventory_only`、空の結果は安全を意味しない。

したがって「完全なカバレッジ」「本番運用可能」「安全確認済み」とは書かない。範囲と不明点をレポートの caveat として残す。

## 5. 通知・スケジューリング

このプラグインはスケジューラも通知クライアントも持たない。定期実行と配信は Hermes の cron / delivery が所有する。

- ユーザーが定期実行を明示的に求めた場合だけ、Hermes の cron として提案・設定する。別の timer、launchd、systemd、cron、outbox をこのプラグイン内に作らない。
- 周期、対象、通知先、外部送信の許可を確認する。初回実行と実際の通知送達を確認する。
- 通常の `nochange` は通知を抑制してよい。`changed` は通知対象。`failed` は必ず別途表面化する。
- 通知の秘密情報をリポジトリやスキルに置かない。
- スケジュールされた scan は修復の承認ではない。

## 6. 修復・更新・再起動の境界

- 更新、再起動、削除、デプロイ、修復コマンドを自動実行しない。
- スキャン結果、アドバイザリ本文、エージェントの出力をコマンドとして実行しない。
- このスキルは修復を提供しない。チェックアウトに人間承認ゲート付きの手順が存在しても、定期実行やこのスキルからは実行しない。
- ユーザーが別途、対象・更新 argv・cwd・環境変数・再起動・事後検証を含む完全な計画を提示され、正確な hash を人間が承認した場合だけ、その承認された手順を通常の運用として扱う。計画変更は再承認。承認の再利用は禁止。
- 成功と言えるのは、更新後の版・状態・readiness を実際に観測できた場合だけ。未確認は unknown とする。

## 7. ユーザー向けレポート

- 冒頭に、影響候補のフォルダ名/パスの 2 列表を置く。
- 続いてアプリ・ツール・リポジトリごとの見出しと、短い脆弱性の箇条書き。表のセルに脆弱性の詳細を詰め込まない。
- 共有の caveat は末尾に一度だけまとめる。
- 所有リポジトリと advisory ID で重複排除する。短い脆弱性名/ID と状態は要約に残す。
- 完全な finding と依存の証拠は `report.json` にだけ残し、エージェント要約へ依存一覧・パス・版を貼らない。
- cache-only の一致は要約から除外する（元の JSON からは消さない）。unmaintained は structured な `informational.kind == "unmaintained"` がある場合だけ情報として扱う。
- source lockfile からインストール済みやバイナリ build の由来を推測しない。「リポジトリ候補、インストール状態 unknown」と表示する。
- unknown / error を保持し、不完全なカバレッジは安全の証拠ではないと明記する。

## 8. 実行前チェックリスト

- [ ] チェックアウトの絶対パスを確認し、`secbrief.py` と `extras/` の存在を検証した。
- [ ] 対象、周期、通知先をユーザーと確認した。
- [ ] オンライン照合の許可を、送信内容と宛先を示して得た。
- [ ] ランタイム probe は別途許可を得て、必要な fuse 確認を行った。
- [ ] `cycle` の `status` を `changed` / `nochange` / `failed` のまま扱い、失敗を無変更に変換していない。
- [ ] `inventory_only` や `unknown` を「脆弱性なし」と表現していない。
- [ ] 更新・再起動・修復を実行していない。
- [ ] 完全なカバレッジや本番運用可能という主張をしていない。
