"""시드 CSV의 채널들 영상 목록(flat)을 받아 통합 DB channel_list에 넣는다. 상세 메타는 받지 않는다.

시드 CSV: config/seed_fusion_teams_candidates.csv (verified=yes, channel_id 있는 행)
이미 목록을 받은 채널(seed_log에 있는 seed)은 건너뛴다.

사용: .venv/bin/python crawl/list_channels.py [--csv ...] [--run v3_fusion] [--seed-type fusion_team]
"""
import argparse
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
sys.path.insert(0, str(ROOT / "process"))
import mock_crawl  # noqa: E402
from mock_analyze import nfc  # noqa: E402
from mock_crawl_v2 import build_name_dict, parse_title  # noqa: E402

DB = ROOT / "db" / "minyo.sqlite"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "config/seed_fusion_teams_candidates.csv"))
    ap.add_argument("--run", default="v3_fusion")
    ap.add_argument("--seed-type", default="fusion_team")
    a = ap.parse_args()
    seeds = pd.read_csv(a.csv)
    seeds = seeds[(seeds.verified.astype(str).str.lower() == "yes") & seeds.channel_id.notna()]
    seeds = seeds.drop_duplicates("channel_id")
    # 기본 시드(mock_crawl_v2.SEEDS)에 이미 있는 채널은 다른 이름으로 또 받지 않는다
    from mock_crawl_v2 import SEEDS
    base = {u.split("/channel/")[1].split("/")[0] for _, _, u in SEEDS if "/channel/" in u}
    seeds = seeds[~seeds.channel_id.isin(base)]
    con = sqlite3.connect(DB)
    known_ch = {r[0] for r in con.execute("SELECT DISTINCT channel_id FROM video WHERE channel_id IS NOT NULL")}
    done = {r[0] for r in con.execute("SELECT seed FROM seed_log")}
    names = build_name_dict()
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "sleep_interval_requests": 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for r in seeds.itertuples():
            seed = str(r.team)
            if seed in done:
                continue
            t0 = time.time()
            url = f"https://www.youtube.com/channel/{r.channel_id}/videos"
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as e:
                mock_crawl.log(f"list {seed}: ERROR {str(e)[:100]}")
                continue
            rows = []
            for e in info.get("entries") or []:
                if not e or not e.get("id"):
                    continue
                title = nfc(e.get("title"))
                p = parse_title(title, names)
                rows.append((a.run, seed, a.seed_type, e["id"], title, e.get("duration"), e.get("view_count"),
                             p[0], p[1], p[2], p[3], "|".join(p[4]), p[5]))
            con.executemany("INSERT OR IGNORE INTO channel_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.execute("INSERT OR REPLACE INTO seed_log VALUES (?,?,?,?,?)",
                        (a.run, seed, len(rows), time.time() - t0, datetime.now(timezone.utc).isoformat()))
            con.commit()
            m = sum(1 for x in rows if x[11])
            flag = " (already-known channel)" if r.channel_id in known_ch else ""
            mock_crawl.log(f"list {seed}: {len(rows)} videos, {m} matched, {time.time() - t0:.0f}s{flag}")
            time.sleep(random.uniform(3, 5))


if __name__ == "__main__":
    main()
