import os
import json
import time
import urllib.parse
import requests
from bs4 import BeautifulSoup

# ==========================================
# 1. 設定項目
# ==========================================
# DiscordのWebhook URL (環境変数から取得、なければ直書きのフォールバック)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/YOUR_WEBHOOK_URL")

# 検索キーワード（ノイズを除外するための「-キーワード」も付与）
# 例：「当選」「当たり」「当たった」を含み、「当たりますように」「懸賞bot」などを除外
KEYWORDS = '(ビール OR アサヒ OR サッポロ OR キリン) (当選 OR 当たり OR 当たった) -当たりますように -応募 -抽選 -懸賞bot -RT'

# 判定しきい値の設定
FEVER_THRESHOLD_MULTIPLIER = 2.0  # 過去平均の何倍以上でフィーバーとみなすか
MIN_POST_COUNT = 5                 # 過去平均に関わらず最低必要な当選報告数（誤検知防止）
COOLDOWN_MINUTES = 60              # 一度通知したら何分間再通知を抑制するか

# 状態保存用キャッシュファイル名（ローカル実行・GitHub Actionsキャッシュ用）
CACHE_FILE = "fever_state.json"


# ==========================================
# 2. Yahoo!リアルタイム検索からのデータ取得
# ==========================================
def get_yahoo_realtime_count(query: str) -> int:
    """
    Yahoo!リアルタイム検索から最新の投稿数（またはヒット件数）を取得する
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://search.yahoo.co.jp/realtime/search?p={encoded_query}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Yahoo!リアルタイム検索のHTML構造から最新投稿要素を取得
        # (※ 投稿一覧のDOM要素を取得してカウント)
        posts = soup.find_all('section', class_=lambda c: c and 'Tweet' in c or 'Post' in c)
        if not posts:
            # セレクタフォールバック：一般的な記事・ツイート枠を取得
            posts = soup.select("div[class*='Tweet']", "div[class*='Post']")
            
        count = len(posts)
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 取得したポスト件数: {count}件")
        return count

    except Exception as e:
        print(f"データ取得エラー: {e}")
        return 0


# ==========================================
# 3. キャッシュ（前回の状態）管理
# ==========================================
def load_cache() -> dict:
    """前回の履歴データ（過去のカウント履歴や最終通知時刻）をロード"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "history": [],         # 過去数回分のカウント履歴
        "last_notified": 0     # 最後に通知したタイムスタンプ(UNIX時間)
    }

def save_cache(data: dict):
    """履歴データを保存"""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ==========================================
# 4. Discord通知機能（Rich Embed対応）
# ==========================================
def send_discord_fever_alert(current_count: int, avg_count: float, search_url: str):
    """Discordへ見やすいカード形式（Embed）で通知を送信"""
    embed = {
        "title": "🔥【フィーバータイム発生中】当選報告の急増を検知！",
        "description": "Yahoo!リアルタイム検索にて、当選報告ポストの急増を検知しました。\n今すぐキャンペーンに挑戦してみてください！",
        "color": 15158332,  # 鮮やかな赤/オレンジ色
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
            }
        ],
        "footer": {
            "text": "Yahoo!リアルタイム検索 監視bot • 20分周期チェック"
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    payload = {
        "content": "@everyone 🍺 **ビール等無料クーポンのフィーバータイムかもしれません！**",
        "embeds": [embed],
        "components": []
    }
    
    # 検索ページへの直リンクボタン案内を追記
    embed["fields"].append({
        "name": "🔗 リアルタイム検索で確認",
        "value": f"[ここをクリックしてXの投稿を確認]({search_url})"
    })

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        res.raise_for_status()
        print("Discordへ通知を送信しました。")
    except Exception as e:
        print(f"Discord通知送信エラー: {e}")


# ==========================================
# 5. メインロジック（定期判定）
# ==========================================
def main():
    search_url = f"https://search.yahoo.co.jp/realtime/search?p={urllib.parse.quote(KEYWORDS)}"
    cache = load_cache()
    
    current_count = get_yahoo_realtime_count(KEYWORDS)
    now = time.time()
    
    # 履歴の更新（直近 6回＝約2時間分 のデータを保持）
    history = cache.get("history", [])
    if current_count > 0:
        history.append(current_count)
    if len(history) > 6:
        history.pop(0)
    
    cache["history"] = history
    
    # 過去の平均値を算出（データが少ない場合はデフォルト値を想定）
    if len(history) > 1:
        # 今回の数値を除いた過去データで平均を計算
        past_counts = history[:-1]
        avg_count = sum(past_counts) / len(past_counts)
    else:
        avg_count = 2.0  # 初期状態の安全なベースライン

    print(f"判定データ: 直近={current_count}件, 過去平均={avg_count:.1f}件")

    # クールダウン期間中か確認
    last_notified = cache.get("last_notified", 0)
    minutes_since_last_notify = (now - last_notified) / 60
    
    # 【フィーバー判定条件】
    # 1. 件数が最低件数（MIN_POST_COUNT）以上
    # 2. 直近件数が過去平均の FEVER_THRESHOLD_MULTIPLIER 倍以上
    # 3. クールダウン時間（COOLDOWN_MINUTES）が経過していること
    is_fever = (current_count >= MIN_POST_COUNT) and (current_count >= avg_count * FEVER_THRESHOLD_MULTIPLIER)
    
    if is_fever:
        if minutes_since_last_notify >= COOLDOWN_MINUTES:
            print(">>> フィーバータイムを検知！通知を実行します。")
            send_discord_fever_alert(current_count, avg_count, search_url)
            cache["last_notified"] = now
        else:
            print(f"フィーバー条件を満たしましたが、クールダウン中のため通知をスキップします（前回通知から {int(minutes_since_last_notify)}分経過 / 抑制 {COOLDOWN_MINUTES}分）。")
    else:
        print("通常状態です。")

    # キャッシュの保存
    save_cache(cache)

if __name__ == "__main__":
    main()
