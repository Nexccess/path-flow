from __future__ import annotations


def subject(store_name: str, store_id: str) -> str:
    return f"{store_name}様向けの来店前受付ページを作成しました"


def footer(store_id: str) -> str:
    return f"""\n\n合同会社Nexccess\nPath-Flow\nEmail: info@nexccess.com\n\n※今後このようなご案内が不要でしたら、その旨ご返信ください。以後のご案内を停止いたします。\n管理番号: PF-{store_id}"""


def initial_body(store_name: str, store_id: str, lp_url: str) -> str:
    return f"""{store_name} 様

初めまして。突然のご連絡失礼いたします。

突然のご連絡の上に誠に勝手ながら、{store_name}様向けのWebサイトを試作してみました。

今回試作したWebサイトは、一般的な営業用サイトではなく、
ご来店前のお客様が、ご自身の希望を簡単に整理できる
「事前受付ページ」として作成したものです。

○ 試作Webサイトはこちらです。
↓↓↓↓
{lp_url}

ご来店前のお客様は、Webサイト上で5つの質問に回答するだけで、
ご自身の希望を整理し、そのまま店舗様への相談・予約につなげることができます。

この仕組みには生成AIを利用していますが、
「AIを導入する」というより、Web上に受付スタッフを一人増やすイメージに近いものです。

営業資料ではなく、実際に操作できるページですので、
よろしければ一度ご覧ください。""" + footer(store_id)


def followup1_body(store_name: str, store_id: str, lp_url: str) -> str:
    return f"""{store_name} 様

先日お送りした{store_name}様向けの受付ページについて、念のため再送いたします。

営業資料ではなく、実際に操作できるページです。
お時間のある際に一度だけ触っていただければ内容をご確認いただけます。

{lp_url}""" + footer(store_id)


def followup2_body(store_name: str, store_id: str, lp_url: str) -> str:
    return f"""{store_name} 様

{store_name}様向けに作成したページについて、最後にご案内だけお送りします。

現時点でご予定がなければ、ご返信は不要です。

{lp_url}""" + footer(store_id)


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
