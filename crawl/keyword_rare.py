"""희귀한 서도·제주 소리 키워드 검색 → 통합 DB channel_list에 seed_type='keyword_rare'로 저장.

대상: 서도·제주 계열 tier 1 곡 중 채널 목록에서 영상이 --max-seen 개 이하인 곡.
검색어: "곡명", "서도민요 곡명"(제주는 "제주민요 곡명"). 결과는 목록(flat)만 받고,
상세 수집은 crawl_v3.py detail --types keyword_rare 로 따로 한다.

사용: .venv/bin/python crawl/keyword_rare.py [--per-query 20 --max-seen 4]
"""
import argparse
import random
import re
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
REGION = {"서도민요": "서도민요", "서도 잡가": "서도민요", "제주민요": "제주민요", "제주도 향토민요": "제주민요"}
RUN = "v3_rare"  # --names를 주면 --run 값으로 바뀜


def targets(con, max_seen):
    songs = pd.read_csv(ROOT / "master/anchor_songs.csv")
    songs = songs[(songs.tier == 1) & songs.detail_genre.isin(REGION)].copy()
    songs["name"] = songs.title_norm.str.replace(r"\s*\d+$", "", regex=True)
    cl = pd.read_sql("SELECT video_id, songs FROM channel_list WHERE songs != ''", con)
    per = cl.assign(s=cl.songs.str.split("|")).explode("s").drop_duplicates(["video_id", "s"]).s.value_counts()
    songs["seen"] = songs.name.map(per).fillna(0).astype(int)
    t = songs[songs.seen <= max_seen].drop_duplicates("name")
    return [(r.name, REGION[r.detail_genre], r.seen) for r in t.itertuples()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-query", type=int, default=20)
    ap.add_argument("--max-seen", type=int, default=4)
    ap.add_argument("--names", default=None, help="쉼표로 구분한 곡명. 주면 이 곡들만 '곡명 민요'로 검색")
    ap.add_argument("--run", default=None)
    ap.add_argument("--seed-type", default="keyword_rare")
    ap.add_argument("--suffix", default="민요", help="--names 검색어 뒤에 붙일 말")
    a = ap.parse_args()
    global RUN
    if a.run:
        RUN = a.run
    con = sqlite3.connect(DB)
    names = build_name_dict()
    tg = ([(n.strip(), a.suffix, -1) for n in a.names.split(",") if n.strip()] if a.names
          else targets(con, a.max_seen))
    mock_crawl.log(f"targets={len(tg)}: {[(n, s) for n, _, s in tg]}")
    done = {r[0] for r in con.execute("SELECT seed FROM seed_log WHERE run=?", (RUN,))}
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "sleep_interval_requests": 1}
    total_new = 0
    with yt_dlp.YoutubeDL(opts) as ydl:
        for name, region, seen in tg:
            for q in ((f"{name} {region}",) if a.names else (name, f"{region} {name}")):
                if q in done:
                    continue
                t0 = time.time()
                try:
                    info = ydl.extract_info(f"ytsearch{a.per_query}:{q}", download=False)
                except yt_dlp.utils.DownloadError as e:
                    mock_crawl.log(f"search '{q}': ERROR {str(e)[:100]}")
                    time.sleep(60)
                    continue
                rows = []
                for e in info.get("entries") or []:
                    if not e or not e.get("id"):
                        continue
                    title = nfc(e.get("title"))
                    p = parse_title(title, names)
                    rows.append((RUN, q, a.seed_type, e["id"], title, e.get("duration"), e.get("view_count"),
                                 p[0], p[1], p[2], p[3], "|".join(p[4]), p[5]))
                con.executemany("INSERT OR IGNORE INTO channel_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                con.execute("INSERT OR REPLACE INTO seed_log VALUES (?,?,?,?,?)",
                            (RUN, q, len(rows), time.time() - t0, datetime.now(timezone.utc).isoformat()))
                con.commit()
                hit = sum(1 for r in rows if name.replace(" ", "") in re.sub(r"\s", "", r[4]))
                total_new += hit
                mock_crawl.log(f"search '{q}': {len(rows)} results, {hit} with '{name}' in title")
                time.sleep(random.uniform(5, 8))
    mock_crawl.log(f"done. titles containing target song: {total_new}")


if __name__ == "__main__":
    main()
