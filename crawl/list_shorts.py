"""시드 채널의 /shorts 탭 목록을 받아 숏츠를 확정한다 → 통합 DB shorts 테이블, video.is_short 갱신.

길이(60초 이하)나 세로 화면만으로는 숏츠를 가릴 수 없어서(2024년부터 최대 3분),
채널이 숏츠로 올린 목록을 직접 대조한다. 채널당 요청 1~몇 번.

사용: .venv/bin/python crawl/list_shorts.py
"""
import random
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
import mock_crawl  # noqa: E402
from mock_crawl_v2 import SEEDS  # noqa: E402

DB = ROOT / "db" / "minyo.sqlite"


def seed_channels():
    ch = {}
    for _, _, url in SEEDS:
        if "/channel/" in url:
            ch[url.split("/channel/")[1].split("/")[0]] = url
    for f in (ROOT / "config").glob("seed_*.csv"):
        d = pd.read_csv(f)
        if "channel_id" in d:
            for c in d.channel_id.dropna():
                ch.setdefault(c, None)
    return sorted(ch)


def main():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS shorts (video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, checked_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS shorts_log (channel_id TEXT PRIMARY KEY, n INTEGER, status TEXT)")
    done = {r[0] for r in con.execute("SELECT channel_id FROM shorts_log")}
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "sleep_interval_requests": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for cid in seed_channels():
            if cid in done:
                continue
            try:
                info = ydl.extract_info(f"https://www.youtube.com/channel/{cid}/shorts", download=False)
                es = [e for e in info.get("entries") or [] if e and e.get("id")]
                status = "ok"
            except yt_dlp.utils.DownloadError as e:
                es, status = [], "no_shorts_tab" if "does not have a shorts tab" in str(e) else "error:" + str(e)[:80]
            now = pd.Timestamp.now().isoformat()
            con.executemany("INSERT OR REPLACE INTO shorts VALUES (?,?,?,?)", [(e["id"], cid, e.get("title"), now) for e in es])
            con.execute("INSERT OR REPLACE INTO shorts_log VALUES (?,?,?)", (cid, len(es), status))
            con.commit()
            mock_crawl.log(f"shorts {cid}: {len(es)} ({status})")
            time.sleep(random.uniform(3, 5))
    # 확정: shorts 목록에 있으면 1, 시드 채널 영상인데 목록에 없으면 0
    con.execute("UPDATE video SET is_short = CASE WHEN video_id IN (SELECT video_id FROM shorts) THEN 1 ELSE 0 END")
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM video WHERE is_short=1").fetchone()[0]
    mock_crawl.log(f"shorts confirmed in collected videos: {n}")


if __name__ == "__main__":
    main()
