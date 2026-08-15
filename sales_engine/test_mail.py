from __future__ import annotations

import argparse

from graph_mail import GraphConfig, GraphMailClient

DEFAULT_TO = "0nakamura.keita@gmail.com"
TEST_STORE_ID = "PF-TEST-001"


def main() -> None:
    p = argparse.ArgumentParser(description="Send exactly one Path-Flow Microsoft Graph test mail.")
    p.add_argument("--to", default=DEFAULT_TO)
    p.add_argument("--live", action="store_true", help="Actually send. Without this flag, dry-run only.")
    args = p.parse_args()

    cfg = GraphConfig.from_env()
    client = GraphMailClient(cfg)

    subject = f"Path-Flow Sales Engine 送受信テスト [PF:{TEST_STORE_ID}]"
    body = f"""Path-Flow Sales Engine の送受信テストです。

送信元: {cfg.mailbox}
テストID: {TEST_STORE_ID}

このメールを受信できたら、そのまま返信してください。
返信検知と HUMAN_ACTION への切替テストに使用します。

※営業メールではありません。
"""

    result = client.send_text(args.to, subject, body, dry_run=not args.live)
    print(result)


if __name__ == "__main__":
    main()
