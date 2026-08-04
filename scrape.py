#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
沖縄県 公募・入札発注情報 スクレイパー

沖縄県公式サイトの「公募・入札発注情報」ページ配下、13の業務カテゴリそれぞれについて
最新の実施年度ページを見つけ、募集中／募集期間終了／結果の各案件を抽出して data.json に保存する。

実行方法:
    pip install -r requirements.txt
    python scrape.py

出力:
    data.json （リポジトリ直下。フロントエンド index.html がこれを fetch して表示する）
"""

import hashlib
import io
import json
import re
import time
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
except ImportError:  # pdfplumber未インストールでもHTML抽出だけは動くようにする
    pdfplumber = None

BASE = "https://www.pref.okinawa.jp"
INDEX_URL = f"{BASE}/shigoto/nyusatsukeiyaku/1015342/index.html"

# 「公募・入札発注情報」トップに載っている13の業務カテゴリ名。
# これに一致するリンクだけをカテゴリページとして扱う（電子入札ポータル等の別システムは対象外）。
CATEGORY_NAMES = [
    "調達（備品・設備・車両・医薬品など）",
    "賃貸借・リース",
    "広報・広告・イベント",
    "調査・検査・収集・運送",
    "研修・訓練・学習・人材育成",
    "会議運営・計画策定・コンサルティング",
    "工事（電子入札ポータル以外）・修繕・製造・設計",
    "警備・清掃・設備点検",
    "施設管理・指定管理・維持管理",
    "情報関連・機器保守点検",
    "売払い・処分・廃棄",
    "観光支援・産業支援・交流",
    "その他・事務の代行",
]

HEADERS = {
    "User-Agent": "okinawa-koubo-tracker/1.0 (+internal team tool; contact: n-hirata052@promo-uruma.com)"
}
REQUEST_INTERVAL_SEC = 1.0  # 県サイトへの負荷軽減のため、リクエスト間に間隔を空ける
TIMEOUT = 20

# 県サイトのHTML構造が変わるとカテゴリリンクを拾えなくなる。そのまま処理を続けると
# 「今回1件も検出できなかった」＝「全案件がサイトから消えた」と解釈され、既存データが
# すべて掲載終了扱いで上書きされてしまう。検出数がこれを下回ったら中断する。
MIN_CATEGORIES = 10

# 詳細ページ本文から金額を拾うときに手がかりにするラベル（出現順に優先）
AMOUNT_LABELS = [
    "予定価格",
    "契約金額",
    "提案限度額",
    "委託料",
    "購入予定金額",
    "調達予定価格",
    "入札金額",
    "落札金額",
    "契約予定金額",
]
AMOUNT_VALUE_RE = re.compile(r"([0-9][0-9,，]*)\s*円")
WAREKI_RE = re.compile(r"令和(\d+)年度")

# 添付ファイルのうち、金額が書かれている可能性が高いものを優先して開く
# （ファイル名に含まれるキーワードでスコアリングする。値が大きいほど優先）
ATTACHMENT_PRIORITY_KEYWORDS = [
    ("入札公告", 100),
    ("公告", 90),
    ("仕様書", 80),
    ("募集要項", 80),
    ("応募要領", 70),
    ("実施要領", 70),
    ("契約書", 40),
]
ATTACHMENT_EXT_RE = re.compile(r"\.(pdf|zip)(?:\?.*)?$", re.IGNORECASE)
MAX_ATTACHMENTS_PER_ITEM = 4  # 1案件あたり開きに行く添付ファイルの上限（負荷・実行時間対策）
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15MBを超えるファイルはスキップ
DATE_RANGE_RE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日.*?[～〜~]\s*(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"
)


def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    time.sleep(REQUEST_INTERVAL_SEC)
    return BeautifulSoup(resp.text, "html.parser")


def find_category_links(soup: BeautifulSoup) -> dict:
    """トップページから13カテゴリの {カテゴリ名: URL} を取得"""
    links = {}
    for a in soup.select("article a[href]"):
        text = a.get_text(strip=True)
        if text in CATEGORY_NAMES:
            links[text] = urljoin(INDEX_URL, a["href"])
    return links


def find_latest_fiscal_year_url(soup: BeautifulSoup, category_url: str) -> tuple:
    """カテゴリページから『令和X年度実施業務』のうち最新のものを選ぶ。戻り値: (年度ラベル, URL)"""
    best_year, best_url, best_label = -1, None, None
    for a in soup.select("article a[href]"):
        text = a.get_text(strip=True)
        m = WAREKI_RE.search(text)
        if m:
            year = int(m.group(1))
            if year > best_year:
                best_year = year
                best_url = urljoin(category_url, a["href"])
                best_label = f"令和{year}年度"
    return best_label, best_url


def split_dept(raw: str):
    """『土木建築部　港湾課』のような表記を (担当部, 担当課) に分割する。
    県サイトの表記は部等と課等の間が全角スペース区切りになっているため、
    最初の空白（全角/半角どちらでも）で分割する。空白がない場合（例：『沖縄県教育庁』単独）は
    担当部のみとして扱い、担当課はNoneにする。"""
    if not raw:
        return None, None
    parts = re.split(r"[\s\u3000]+", raw.strip(), maxsplit=1)
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def parse_period(raw: str):
    """『2026年8月4日（火曜日）～2026年8月14日（金曜日）』→ (開始ISO, 終了ISO)。
    終了側に年が省略されている場合（例: 8月14日のみ）は開始年を補う。"""
    m = DATE_RANGE_RE.search(raw.replace("〜", "～").replace("~", "～"))
    if not m:
        return None, None
    y1, m1, d1, y2, m2, d2 = m.groups()
    start = f"{int(y1):04d}-{int(m1):02d}-{int(d1):02d}"
    end_year = int(y2) if y2 else int(y1)
    end = f"{end_year:04d}-{int(m2):02d}-{int(d2):02d}"
    return start, end


def search_amount_in_text(text: str):
    """任意のテキスト（HTML本文 or PDF抽出テキスト）から金額らしき記述を探す。
    見つからなければ (None, None, None)。"""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        for label in AMOUNT_LABELS:
            if label in line:
                # 同じ行、または直後の1〜2行以内に金額が来るケースをカバー
                for candidate in lines[i : i + 3]:
                    m = AMOUNT_VALUE_RE.search(candidate)
                    if m:
                        raw = m.group(0)
                        digits = m.group(1).replace(",", "").replace("，", "")
                        try:
                            yen = int(digits)
                        except ValueError:
                            yen = None
                        return label, raw, yen
    return None, None, None


def extract_amount_from_detail(detail_soup: BeautifulSoup):
    """詳細ページ本文のテキストから金額を探す"""
    article = detail_soup.select_one("article") or detail_soup
    return search_amount_in_text(article.get_text("\n"))


def find_attachment_links(detail_soup: BeautifulSoup, page_url: str):
    """詳細ページ記事内から .pdf / .zip への添付リンクを、金額が書かれていそうな順に並べて返す"""
    article = detail_soup.select_one("article") or detail_soup
    candidates = []
    for a in article.select("a[href]"):
        href = a.get("href", "")
        if not ATTACHMENT_EXT_RE.search(href):
            continue
        text = a.get_text(strip=True)
        score = 0
        for kw, pts in ATTACHMENT_PRIORITY_KEYWORDS:
            if kw in text:
                score = max(score, pts)
        candidates.append((score, urljoin(page_url, href), text))
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[:MAX_ATTACHMENTS_PER_ITEM]


def extract_text_from_pdf_bytes(data: bytes) -> str:
    if pdfplumber is None:
        return ""
    text_parts = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                text_parts.append(t)
    except Exception as e:  # noqa: BLE001
        print(f"    [警告] PDF解析失敗: {e}", file=sys.stderr)
    return "\n".join(text_parts)


def search_amount_in_attachment(url: str):
    """PDF、またはZIP内に入っているPDFを開いて金額を探す。見つからなければ (None, None, None, None)。
    戻り値の最後の要素は「どのファイルで見つかったか」（表示・デバッグ用）。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        time.sleep(REQUEST_INTERVAL_SEC)
    except Exception as e:  # noqa: BLE001
        print(f"    [警告] 添付ファイル取得失敗: {url} ({e})", file=sys.stderr)
        return None, None, None, None

    if len(resp.content) > MAX_ATTACHMENT_BYTES:
        print(f"    [情報] サイズ上限超過のためスキップ: {url}", file=sys.stderr)
        return None, None, None, None

    filename = url.rsplit("/", 1)[-1]

    if url.lower().endswith(".pdf"):
        text = extract_text_from_pdf_bytes(resp.content)
        label, raw, yen = search_amount_in_text(text)
        if label:
            return label, raw, yen, filename
        return None, None, None, None

    if url.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
                for name in pdf_names:
                    try:
                        pdf_bytes = zf.read(name)
                    except Exception:  # noqa: BLE001
                        continue
                    text = extract_text_from_pdf_bytes(pdf_bytes)
                    label, raw, yen = search_amount_in_text(text)
                    if label:
                        return label, raw, yen, f"{filename}/{name}"
        except zipfile.BadZipFile:
            print(f"    [警告] ZIPとして開けませんでした: {url}", file=sys.stderr)
        return None, None, None, None

    return None, None, None, None


def parse_fiscal_year_page(soup: BeautifulSoup, page_url: str, category: str, fiscal_label: str):
    """『募集中/募集期間終了/結果』の見出しと、その下にある入札/公募テーブルを順に辿って案件リストを作る"""
    items = []
    article = soup.select_one("article") or soup
    current_status = None

    for el in article.find_all(["h2", "table"]):
        if el.name == "h2":
            current_status = el.get_text(strip=True)
            continue
        if el.name == "table":
            caption_el = el.find("caption")
            kind = caption_el.get_text(strip=True) if caption_el else None
            headers = [th.get_text(strip=True) for th in el.select("tr th")]
            for row in el.select("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                link_el = cells[0].find("a")
                title = (link_el or cells[0]).get_text(strip=True)
                url = urljoin(page_url, link_el["href"]) if link_el else None
                period_or_date_raw = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                dept_raw = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                dept_division, dept_section = split_dept(dept_raw)

                start_iso, end_iso = (None, None)
                open_date_iso = None
                if headers and "開札日" in headers[1] if len(headers) > 1 else False:
                    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", period_or_date_raw)
                    if m:
                        y, mo, d = m.groups()
                        open_date_iso = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
                else:
                    start_iso, end_iso = parse_period(period_or_date_raw)

                items.append(
                    {
                        "category": category,
                        "fiscal_year": fiscal_label,
                        "status": current_status,
                        "type": kind,
                        "title": title,
                        "url": url,
                        "dept": dept_raw,
                        "dept_division": dept_division,
                        "dept_section": dept_section,
                        "period_raw": period_or_date_raw,
                        "period_start": start_iso,
                        "period_end": end_iso,
                        "open_date": open_date_iso,
                    }
                )
    return items


def enrich_with_amount(item: dict):
    """募集中の案件のみ、詳細ページ→（見つからなければ）添付PDF/ZIPの順に金額を探す
    （結果・終了案件はスキップして負荷を抑える）"""
    item["amount_label"] = None
    item["amount_raw"] = None
    item["amount_yen"] = None
    item["amount_source"] = None  # "本文" or 添付ファイル名。UIでの表示や検証に使う

    if item["status"] != "募集中" or not item["url"]:
        return item

    try:
        detail = fetch(item["url"])
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 詳細ページ取得失敗: {item['url']} ({e})", file=sys.stderr)
        return item

    label, raw, yen = extract_amount_from_detail(detail)
    if label:
        item["amount_label"], item["amount_raw"], item["amount_yen"] = label, raw, yen
        item["amount_source"] = "本文"
        return item

    # 本文に金額の記載がなければ、添付のPDF/ZIPを優先度順に開いて探す
    for score, att_url, att_text in find_attachment_links(detail, item["url"]):
        label, raw, yen, source_file = search_amount_in_attachment(att_url)
        if label:
            item["amount_label"], item["amount_raw"], item["amount_yen"] = label, raw, yen
            item["amount_source"] = f"添付: {att_text or source_file}"
            break

    return item


def item_key(item: dict) -> str:
    """案件を一意に識別するキー。URLがあればURLを使い、無ければ主要項目からハッシュを作る
    （同じ案件なら再実行しても同じキーになるようにする）。"""
    if item.get("url"):
        return item["url"]
    basis = "|".join(
        [
            item.get("category") or "",
            item.get("title") or "",
            item.get("dept") or "",
            item.get("period_start") or item.get("open_date") or "",
        ]
    )
    return "nokey:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def main():
    print("既存の data.json を読み込み中...")
    try:
        with open("data.json", encoding="utf-8") as f:
            previous = json.load(f)
        previous_items = {item_key(it): it for it in previous.get("items", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        previous_items = {}
    print(f"  既存 {len(previous_items)} 件")

    print("トップページ取得中...")
    top_soup = fetch(INDEX_URL)
    category_links = find_category_links(top_soup)
    print(f"  {len(category_links)}/{len(CATEGORY_NAMES)} カテゴリを検出")

    if len(category_links) < MIN_CATEGORIES:
        print(
            f"[中断] カテゴリ検出数 {len(category_links)} 件が下限 {MIN_CATEGORIES} 件を下回りました。"
            f"サイト構造が変わった可能性があるため data.json は更新しません。",
            file=sys.stderr,
        )
        sys.exit(1)

    fresh_items = []
    for category, cat_url in category_links.items():
        print(f"[{category}] 年度ページを検索中...")
        cat_soup = fetch(cat_url)
        fiscal_label, fy_url = find_latest_fiscal_year_url(cat_soup, cat_url)
        if not fy_url:
            print("  最新年度ページが見つかりませんでした。スキップします。")
            continue
        print(f"  最新年度: {fiscal_label} -> {fy_url}")
        fy_soup = fetch(fy_url)
        items = parse_fiscal_year_page(fy_soup, fy_url, category, fiscal_label)
        print(f"  {len(items)} 件取得。募集中案件の金額を確認中...")
        items = [enrich_with_amount(it) for it in items]
        fresh_items.extend(items)

    # カテゴリは拾えたのに案件が1件も取れないのも、テーブル構造の変更を疑うべき状態。
    # 既存データを掲載終了扱いで潰さないよう、ここでも中断する。
    if not fresh_items and previous_items:
        print(
            "[中断] 案件を1件も抽出できませんでした。既存データを保護するため "
            "data.json は更新しません。",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── 既存データとマージ（サイト上から消えた案件も履歴として残す） ──
    now_iso = datetime.now(timezone(timedelta(hours=9))).isoformat()
    fresh_keys = set()
    merged = []

    for it in fresh_items:
        key = item_key(it)
        fresh_keys.add(key)
        prev = previous_items.get(key)
        it["first_seen_at"] = prev["first_seen_at"] if prev and prev.get("first_seen_at") else now_iso
        it["last_seen_at"] = now_iso
        it["still_listed"] = True
        merged.append(it)

    removed_count = 0
    for key, prev in previous_items.items():
        if key in fresh_keys:
            continue
        # サイト上には無くなったが、トラッカーには残す
        prev["still_listed"] = False
        prev.setdefault("first_seen_at", prev.get("last_seen_at", now_iso))
        merged.append(prev)
        removed_count += 1

    print(f"マージ結果: 今回検出 {len(fresh_items)} 件 / サイトから消えて履歴として保持 {removed_count} 件")

    jst = timezone(timedelta(hours=9))
    output = {
        "updated_at": datetime.now(jst).isoformat(),
        "source": INDEX_URL,
        "item_count": len(merged),
        "items": merged,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"完了: 合計 {len(merged)} 件（うち現在サイトに掲載中 {len(fresh_items)} 件）を data.json に保存しました。")


if __name__ == "__main__":
    main()
