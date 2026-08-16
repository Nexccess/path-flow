from __future__ import annotations


def subject(store_name: str, store_id: str) -> str:
    return f"{store_name}様向けの来店前受付ページについて"


def footer(store_id: str) -> str:
    return """

――――――――――
Nexccess
中村景太

Mail：info@nexccess.com
――――――――――"""


def initial_body(store_name: str, store_id: str, lp_url: str) -> str:
    return f"""突然の営業連絡失礼いたします。

Nexccessの中村と申します。

美容サロン様向けに、
お客様の来店前ヒアリングを簡単にする
受付ページのサービスを提供しております。

{store_name}様向けにサンプルページを作成いたしました。

実際のページ：
{lp_url}

来店前にお客様の希望メニューや要望を整理できるため、
店舗様の対応負担軽減につながる仕組みです。

もしご興味がございましたら、
詳細をご説明させていただけますと幸いです。

よろしくお願いいたします。
""" + footer(store_id)


def followup1_body(store_name: str, store_id: str, lp_url: str) -> str:
    return f"""{store_name}様

先日ご案内した来店前受付ページについて、
念のため再度ご連絡いたしました。

実際に操作できるサンプルページですので、
お時間のある際にご確認いただけますと幸いです。

{lp_url}
""" + footer(store_id)


def followup2_body(store_name: str, store_id: str, lp_url: str) -> str:
    return f"""{store_name}様

以前ご案内した受付ページについて、
最後のご連絡となります。

ご不要の場合はご返信不要です。

{lp_url}
""" + footer(store_id)


def render(stage: str, store_name: str, store_id: str, lp_url: str) -> tuple[str, str]:
    if stage == "initial":
        body = initial_body(store_name, store_id, lp_url)
    elif stage == "followup1":
        body = followup1_body(store_name, store_id, lp_url)
    elif stage == "followup2":
        body = followup2_body(store_name, store_id, lp_url)
    else:
        raise ValueError(f"Unknown stage: {stage}")

    return subject(store_name, store_id), body