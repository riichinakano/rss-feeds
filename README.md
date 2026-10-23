# rss-feeds

RSSを配信していないサイトの一覧ページを1日2回（6:00 / 18:00 JST）読み込み、新着記事のまとめをRSSとして配信する。
Feedly等で購読する前提で、閲覧画面・データベースは持たない。

## 購読URL

| サイト | 購読URL |
|---|---|
| Morning AI | https://riichinakano.github.io/rss-feeds/morningai.xml |

## 仕組み

- `sites.yaml` … 対象サイトとCSSセレクタの定義（サイト追加はここに追記）
- `build.py` … 一覧ページを取得 → 新着記事を抽出 → 1回の更新につき1件のまとめ記事を作り、`docs/<id>.xml` に書き出す
- `state/<id>.json` … 掲載済みURL（新着判定用）と直近14件のまとめ記事
- `.github/workflows/build.yml` … 定期実行。取得件数が0件のサイトがあるとワークフローが失敗し、通知メールが届く
- `docs/` を GitHub Pages で公開

## ローカルでの確認

```bash
pip install -r requirements.txt
python build.py --site morningai --dry-run   # 抽出結果を表示するだけ
```
