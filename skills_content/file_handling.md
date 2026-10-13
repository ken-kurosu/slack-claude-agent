---
name: file-handling
description: 添付ファイルの処理ルールをガイドする。「このファイル見て」「CSV分析して」等でファイルが添付された場合に使用。
capabilities: 添付ファイル読取案内, 保存先活用, execute_code読込例提示, 再ダウンロード案内
limitations: ファイル自動修正, 非添付ファイル探索, 巨大ファイル全件目視確認
---

## 添付ファイルの処理ルール

1. ファイルの内容（プレビュー or 全文）がメッセージに自動付加される
2. CSV/JSON/TSV等のデータファイルはワークスペースにも自動保存される
3. 「ワークスペース保存先」が表示された場合、そのパスで `execute_code` から直接読み込める
4. 大きなファイルはサンプル表示なので、全データ分析には必ず `execute_code` を使う
5. `execute_code` でファイルを読む例:
   ```python
   import pandas as pd
   df = pd.read_csv("保存先のパス")
   print(df.shape)
   print(df.head(10))
   ```
6. ファイルが読み込めない場合は `download_slack_file` ツールで再ダウンロードを試みる
