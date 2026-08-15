from __future__ import annotations


def subject(store_name: str, store_id: str) -> str:
    return f"{store_name}様向けの来店前受付ページを作成しました [PF:{store_id}]"


def initial_body(store_name: str, lp_url: str) -> str:
    return f"""{store_name} 様

突然のご連絡失礼いたします。

ネイルサロン向けに、来店前のお客様の希望を整理する受付ページを制作しており、{store_name}様向けのサンプルを作成しました。

{lp_url}

お客様に5つほど質問し、
「どんなネイルにしたいのか」
「予約前に何を相談したいのか」
を整理して、そのまま店舗様への相談・予約につなぐ仕組みです。

AIを導入していただくというより、Web上の受付スタッフを一人増やすイメージに近いものです。

現時点で費用が発生するものではありませんので、まず実際のページをご覧いただければと思います。

もし「うちで使うなら少し調整したい」と感じていただけましたら、その際にご連絡ください。

Path-Flow
"""


def followup1_body(store_name: str, lp_url: str) -> str:
    return f"""{store_name} 様

先日お送りした{store_name}様向けの受付ページについて、念のため再送いたします。

営業資料ではなく、実際に操作できるページです。
お時間のある際に一度だけ触っていただければ内容をご確認いただけます。

{lp_url}

Path-Flow
"""


def followup2_body(store_name: str, lp_url: str) -> str:
    return f"""{store_name} 様

{store_name}様向けに作成したページについて、最後にご案内だけお送りします。

現時点でご予定がなければ、ご返信は不要です。

{lp_url}

Path-Flow
"""


def render(stage: str, store_name: str, store_id: str, lp_url: str) -> tuple[str, str]:
    if stage == "initial":
        body = initial_body(store_name, lp_url)
    elif stage == "followup1":
        body = followup1_body(store_name, lp_url)
    elif stage == "followup2":
        body = followup2_body(store_name, lp_url)
    else:
        raise ValueError(f"Unknown stage: {stage}")
    return subject(store_name, store_id), body
