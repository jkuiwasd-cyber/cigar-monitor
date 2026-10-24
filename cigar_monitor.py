#!/usr/bin/env python3
"""
Karibik-Shop 古巴雪茄库存与新品监控通知脚本
支持本地常驻运行与云端（GitHub Actions / Linux Crontab）定时执行。
检测到新品上架、缺货补货或库存数量变化时，自动向 jkuiwasd@gmail.com 发送邮件提醒。
"""

import os
import re
import sys
import json
import time
import smtplib
import argparse
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from bs4 import BeautifulSoup

# ======================== 配置区域 ========================

# 监控目标配置
BASE_URL = "https://karibik-shop.ch/herkunft/kuba/page/{}/"
MAX_PAGES = 30  # 最大扫描页数（目前共29页，设为30作为保护上限）
CACHE_FILE = "cigar_inventory_cache.json"  # 本地/云端缓存文件
CHECK_INTERVAL_SECONDS = 3600  # 循环检查间隔：3600秒（1小时）

# 邮件发送配置：优先从系统环境变量读取（便于云端部署与密钥保护），未读取到则使用缺省值
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "your_sender_account@gmail.com")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "xxxx xxxx xxxx xxxx")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "jkuiwasd@gmail.com")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}

# ========================================================


def extract_stock_from_raw_text(raw_text: str) -> str:
    """
    直接从商品卡片的纯文本中提取 Karibik-Shop 特有的规格与库存表：
    格式如：'1 CHF 26.10 42 Stk. verfügbar ; 10 CHF 261.00 13 Stk. verfügbar'
    """
    if not raw_text:
        return "未知库存"

    match = re.search(r"Einheit\s+Preis\s+Warenkorb\s+(.*?)\s+Warenkorb anzeigen", raw_text)
    if match:
        block = match.group(1)
        items = [x.strip() for x in re.findall(r".*?verfügbar", block)]
        if items:
            return " ; ".join(items)
        return block.strip()

    # 备用正则：直接抓取所有包含 verfügbar 的片段
    fallback_items = [x.strip() for x in re.findall(r"(?:\d+\s*Stk\.\s*verfügbar|Momentan nicht verfügbar)", raw_text)]
    if fallback_items:
        return " ; ".join(fallback_items)

    return "未显示具体分项库存"


def fetch_page_products(page_num: int):
    """抓取单页商品数据"""
    url = BASE_URL.format(page_num)
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        if response.status_code == 404:
            return None  # 已到达最后一页
        if response.status_code != 200:
            print(f"[警告] 第 {page_num} 页请求失败，HTTP 状态码: {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        products = soup.select(".product")
        if not products:
            return None

        page_data = {}
        for p in products:
            # 提取商品标题
            title_node = p.select_one(".woocommerce-loop-product__title, .product-title, h2, h3")
            if not title_node:
                continue
            name = title_node.get_text(strip=True)

            # 提取商品链接
            link_node = p.select_one("a[href]")
            link = link_node["href"] if link_node else url

            # 提取价格区间
            price_node = p.select_one(".price")
            price = price_node.get_text(" ", strip=True) if price_node else "价格未注明"

            # 提取完整文本并用正则精准提取每个包装规格的实时库存数量
            raw_text = p.get_text(" ", strip=True)
            stock_desc = extract_stock_from_raw_text(raw_text)

            page_data[name] = {
                "name": name,
                "url": link,
                "price": price,
                "stock": stock_desc,
                "raw_text": raw_text
            }

        return page_data

    except Exception as exc:
        print(f"[错误] 抓取第 {page_num} 页时异常: {exc}")
        return []


def scan_all_inventory():
    """扫描全部页面的古巴雪茄"""
    all_products = {}
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始扫描全站古巴雪茄库存...")

    for page in range(1, MAX_PAGES + 1):
        print(f"  正在扫描第 {page} 页...", end="\r")
        page_products = fetch_page_products(page)
        if page_products is None:
            print(f"\n  扫描完成：共检测到 {page - 1} 页。")
            break
        if isinstance(page_products, dict):
            all_products.update(page_products)

        time.sleep(1.2)  # 温和抓取，防止触发商家防护或高频封禁

    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 扫描结束，当前在售/展示商品总数: {len(all_products)} 款。")
    return all_products


def diff_inventory(old_inv: dict, new_inv: dict):
    """
    比对两次抓取的库存差异：
    1. new_items: 全新上架的品种
    2. restocked_items: 库存状态或数量发生任何变化的品种（含补货、数量增减）
    """
    new_items = []
    restocked_items = []

    for name, data in new_inv.items():
        # 1. 发现全新商品品种
        if name not in old_inv:
            new_items.append(data)
            continue

        # 2. 检查现有商品库存状态或数量变化
        old_data = old_inv[name]
        old_stock = old_data.get("stock", "")
        # 兼容旧版缓存：如果旧缓存里是“未显示具体分项库存”，自动从旧缓存的 raw_text 中还原真实库存
        if not old_stock or old_stock == "未显示具体分项库存":
            old_stock = extract_stock_from_raw_text(old_data.get("raw_text", ""))

        new_stock = data.get("stock", "")

        # 只要具体库存文本（包含各规格数量或可用状态）发生变化，即记录并通知
        if old_stock != new_stock:
            restocked_items.append({
                "name": name,
                "url": data["url"],
                "price": data["price"],
                "before": old_stock,
                "now": new_stock
            })

    return new_items, restocked_items


def send_email_alert(new_items, restocked_items):
    """构建并发送 HTML 格式的提醒邮件"""
    if not new_items and not restocked_items:
        return

    subject = f"【古巴雪茄库存提醒】发现 {len(new_items)} 款新品上架 / {len(restocked_items)} 款库存数量变动"

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #b22222;">Karibik-Shop 古巴雪茄库存变动通知</h2>
        <p>监控时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """

    if new_items:
        html_content += """
        <h3 style="background-color: #fce4e4; padding: 8px; border-left: 4px solid #d9534f;">
            ✨ 新增上架品种（共 {} 款）：
        </h3>
        <ul>
        """.format(len(new_items))
        for item in new_items:
            html_content += f"""
            <li style="margin-bottom: 12px;">
                <strong><a href="{item['url']}" style="color: #0066cc; text-decoration: none;">{item['name']}</a></strong><br>
                <span>💰 价格：{item['price']}</span><br>
                <span>📦 库存详情：{item['stock']}</span>
            </li>
            """
        html_content += "</ul>"

    if restocked_items:
        html_content += """
        <h3 style="background-color: #e8f4f8; padding: 8px; border-left: 4px solid #5bc0de;">
            🔄 库存数量 / 状态变动（共 {} 款）：
        </h3>
        <ul>
        """.format(len(restocked_items))
        for item in restocked_items:
            html_content += f"""
            <li style="margin-bottom: 12px;">
                <strong><a href="{item['url']}" style="color: #0066cc; text-decoration: none;">{item['name']}</a></strong><br>
                <span>💰 价格：{item['price']}</span><br>
                <span>📉 之前：[{item['before']}]</span><br>
                <span>📈 现在：[<strong>{item['now']}</strong>]</span>
            </li>
            """
        html_content += "</ul>"

    html_content += """
        <hr style="border: none; border-top: 1px solid #eee; margin-top: 20px;">
        <p style="font-size: 12px; color: #888;">本邮件由古巴雪茄云端自动监控程序发送。</p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 正在发送邮件通知至 {RECEIVER_EMAIL}...")
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], msg.as_string())
        server.quit()
        print("  [成功] 邮件通知发送成功！")
    except Exception as exc:
        print(f"  [失败] 邮件发送失败: {exc}")
        print("  提示：请确保在脚本环境变量或代码中配置了正确的发件 Gmail 和 16 位应用密码。")


def load_cached_inventory():
    """读取上一次保存的库存快照"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[警告] 读取缓存文件失败: {e}")
    return {}


def save_cached_inventory(data: dict):
    """保存本次库存快照到本地或云端文件"""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] 写入缓存文件失败: {e}")


def execute_scan_cycle(is_first_run: bool, cached_inventory: dict):
    """执行单次完整的抓取与比对流程"""
    current_inventory = scan_all_inventory()

    if current_inventory:
        if is_first_run:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 首次初始化运行，已基准记录 {len(current_inventory)} 款古巴雪茄数据。")
            save_cached_inventory(current_inventory)
            return False, current_inventory
        else:
            new_items, restocked_items = diff_inventory(cached_inventory, current_inventory)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 比对结果: 新上架 {len(new_items)} 款, 库存数量变动 {len(restocked_items)} 款。")

            if new_items or restocked_items:
                send_email_alert(new_items, restocked_items)

            save_cached_inventory(current_inventory)
            return False, current_inventory
    else:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 本次未能抓取到商品，跳过比对并保留旧缓存。")
        return is_first_run, cached_inventory


def main():
    parser = argparse.ArgumentParser(description="Karibik-Shop 古巴雪茄监控程序")
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行单次扫描比对后退出（适用于云端 GitHub Actions / Crontab 定时调度）"
    )
    args = parser.parse_args()

    run_once = args.once or os.environ.get("RUN_ONCE", "").lower() in ("true", "1")

    print("=" * 60)
    print(" Karibik-Shop 古巴雪茄监控程序已启动")
    print(f" 目标分类: https://karibik-shop.ch/herkunft/kuba/")
    print(f" 接收邮箱: {RECEIVER_EMAIL}")
    print(f" 运行模式: {'云端定时模式 (执行一次后退出)' if run_once else '常驻循环模式 (每小时自动扫描)'}")
    print("=" * 60)

    cached_inventory = load_cached_inventory()
    is_first_run = (len(cached_inventory) == 0)

    if run_once:
        execute_scan_cycle(is_first_run, cached_inventory)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 单次扫描比对任务完成，退出程序。")
        sys.exit(0)

    while True:
        try:
            is_first_run, cached_inventory = execute_scan_cycle(is_first_run, cached_inventory)
        except Exception as loop_err:
            print(f"[系统异常] 主循环发生错误: {loop_err}")

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 等待 1 小时后进行下一次扫描...\n")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
