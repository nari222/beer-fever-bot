import os
import json
import time
import urllib.parse
import requests
from bs4 import BeautifulSoup

# ==========================================
# 1. 設定項目
# ==========================================
# DiscordのWebhook URL
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL")

# 共通の除外キーワード（ノイズ対策）
COMMON_EXCLUDES = "-当たりますように -応募 -抽選 -懸賞bot -RT"
COMMON_HITS = "(当選 OR 当たり OR 当たった)"

# 監視したい「商品（キャンペーン）リスト」
# 各商品ごとに個別の判定パラメータを設定可能
TARGET_PRODUCTS = [
    {
        "id": "asahi_superdry",
        "name": "一番搾り 10.13",
        "keywords": f'(一番搾り) {COMMON_HITS} {COMMON_EXCLUDES}',
        "min_count": 3,           # 最低発火件数
        "threshold_multiplier": 2.0, # 平均の何倍でフィーバーとするか
        "cooldown_minutes": 60     # 通知後のクールダウン時間（分）
    },
    {
        "id": "suntory_premal",
        "name": "金麦　11.4 10.27",
        "keywords": f'(金麦) {COMMON_HITS} {COMMON_EXCLUDES}',
        "min_count": 3,
        "threshold_multiplier": 2.0,
        "cooldown_minutes": 60
    },
    {
        "id": "suntory_premal",
        "name": "グッドエール　10.12",
        "keywords": f'(グッドエール) {COMMON_HITS} {COMMON_EXCLUDES}',
        "min_count": 3,
        "threshold_multiplier": 2.0,
        "cooldown_minutes": 60
    },
    {
        "id": "sapporo_blacklabel",
        "name": "晴風　10.12",
        "keywords": f'(晴れ風) {COMMON_HITS} {COMMON_EXCLUDES}',
        "min_count": 3,
        "threshold_multiplier": 2.0,
        "cooldown_minutes": 60
    }
]

# キャッシュ保存ファイル
CACHE_FILE = "fever_state.json"


# ==========================================
# 2. Yahoo!リアルタイム検索からのデータ取得
# ==========================================
def get_yahoo_realtime_count(query: str, time_window_minutes: int = 20) -> int:
    """
    指定した直近 N分間（デフォルト20分）に絞り込んで投稿数をカウントする
    """
    now = int(time.time())
    since = now - (time_window_minutes * 60)
    until = now

    encoded_query = urllib.parse.quote(query)
    
    # since / until / gm=h / samplingRate=100 パラメータを動的に付与
    url = (
        f"https://search.yahoo.co.jp/realtime/search"
        f"?p={encoded_query}"
        f"&since={since}"
        f"&until={until}"
        f"&gm=h"
        f"&samplingRate=100"
    )
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 投稿要素を取得
        posts = soup.find_all('section', class_=lambda c: c and 'Tweet' in c or 'Post' in c)
        if not posts:
            posts = soup.select("div[class*='Tweet']", "div[class*='Post']")
            
        print(f"[{time.strftime('%H:%M:%S')}] 直近{time_window_minutes}分間の指定件数: {len(posts)}件")
        return len(posts)

    except Exception as e:
        print(f"データ取得エラー ({query}): {e}")
        return 0


# ==========================================
# 3. キャッシュ（商品ごとの状態）管理
# ==========================================
def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(data: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==========================================
# 4. Discord通知機能（商品名入りEmbed）
# ==========================================
def send_discord_fever_alert(product_name: str, current_count: int, avg_count: float, search_url: str):
    embed = {
        "title": f"🔥【フィーバー通知】{product_name}",
        "description": f"**{product_name}** の当選報告ポストが急増中です！\n今すぐ抽選を引いてみてください！",
        "color": 15158332,  # 赤/オレンジ色
        "fields": [
            {
                "name": "📊 直近20分間の検知数",
                "value": f"**{current_count}件**",
                "inline": True
            },
            {
                "name": "📈 通常時の平均",
                "value": f"{avg_count:.1f}件",
                "inline": True
            },
            {
                "name": "⚡ 急増率",
                "value": f"通常時の **{(current_count / max(avg_count, 1.0)):.1f}倍**",
                "inline": True
            },
            {
                "name": "🔗 X（旧Twitter）で確認",
                "value": f"[リアルタイム検索を開く]({search_url})"
            }
        ],
        "footer": {
            "text": "Yahoo!リアルタイム検索 監視bot • 20分周期"
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    payload = {
        "content": f"@everyone 🍺 **{product_name}** のフィーバータイムかもしれません！",
        "embeds": [embed]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        res.raise_for_status()
        print(f"[{product_name}] Discordへ通知を送信しました。")
    except Exception as e:
        print(f"[{product_name}] Discord通知送信エラー: {e}")


# ==========================================
# 5. メイン判定ロジック
# ==========================================
def main():
    cache = load_cache()
    now = time.time()

    print(f"=== 監視開始: {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

    for product in TARGET_PRODUCTS:
        p_id = product["id"]
        p_name = product["name"]
        keywords = product["keywords"]
        min_count = product["min_count"]
        multiplier = product["threshold_multiplier"]
        cooldown = product["cooldown_minutes"]

        # 商品ごとのキャッシュ構造を取得（存在しなければ初期化）
        p_cache = cache.get(p_id, {"history": [], "last_notified": 0})
        history = p_cache.get("history", [])
        last_notified = p_cache.get("last_notified", 0)

        # 検索実行
        current_count = get_yahoo_realtime_count(keywords)
        print(f"\n【{p_name}】")
        print(f" - 直近件数: {current_count}件")

        # 履歴の更新（過去2時間＝6回分を保持）
        if current_count > 0:
            history.append(current_count)
        if len(history) > 6:
            history.pop(0)

        p_cache["history"] = history

        # 過去の平均値を算出
        if len(history) > 1:
            past_counts = history[:-1]
            avg_count = sum(past_counts) / len(past_counts)
        else:
            avg_count = 2.0  # 初期デフォルト値

        print(f" - 過去平均: {avg_count:.1f}件")

        # クールダウン確認
        minutes_since_last = (now - last_notified) / 60

        # フィーバー判定
        is_fever = (current_count >= min_count) and (current_count >= avg_count * multiplier)

        if is_fever:
            if minutes_since_last >= cooldown:
                print(f" -> 🔥 フィーバー検知！通知を送信します。")
                search_url = f"https://search.yahoo.co.jp/realtime/search?p={urllib.parse.quote(keywords)}"
                send_discord_fever_alert(p_name, current_count, avg_count, search_url)
                p_cache["last_notified"] = now
            else:
                print(f" -> フィーバー条件達成ですが、クールダウン中です（前回通知から {int(minutes_since_last)}分経過 / 抑制 {cooldown}分）。")
        else:
            print(" -> 通常状態です。")

        # キャッシュ書き戻し
        cache[p_id] = p_cache

        # Yahooへの連続アクセス負荷を軽減するため数秒待機
        time.sleep(30)

    # 全商品の状態を保存
    save_cache(cache)
    print("\n=== 監視完了 ===")

if __name__ == "__main__":
    main()
