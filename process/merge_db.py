"""수집 결과를 통합 DB 하나로 합친다: db/minyo.sqlite (+ db/raw/{video_id}.json.gz).

원본(mock/mock.sqlite, mock_v2/mock_v2.sqlite)은 지우지 않는다. 다시 실행해도 같은 결과가 나온다.

테이블
  video          영상 1개 = 1행 (video_id 기준 중복 없음). 상세 메타가 있는 영상만
  video_source   어떤 수집 회차·경로로 그 영상을 만났는지 (영상 1개가 여러 행 가능)
  channel_list   채널 순회로 본 목록 전체 (상세를 안 받은 영상 포함)
  subtitle       자막 텍스트
  queries, seed_log   회차별 검색어·채널 목록 기록

사용: .venv/bin/python process/merge_db.py
"""
import shutil
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
import mock_crawl  # noqa: E402  (video·subtitle 스키마 재사용)

DB = ROOT / "db" / "minyo.sqlite"
SOURCES = [  # (회차 이름, DB 경로, raw 폴더)
    ("mock_v1", ROOT / "mock/mock.sqlite", ROOT / "mock/raw"),
    ("mock_v2", ROOT / "mock_v2/mock_v2.sqlite", ROOT / "mock_v2/raw"),
]

SCHEMA = mock_crawl.SCHEMA.replace("CREATE TABLE IF NOT EXISTS queries (\n  query TEXT PRIMARY KEY,",
                                   "CREATE TABLE IF NOT EXISTS queries (\n  run TEXT, query TEXT,") + """
CREATE TABLE IF NOT EXISTS video_source (
  video_id TEXT, run TEXT, route_type TEXT, route TEXT, rank INTEGER,
  pattern TEXT, songs TEXT, match_source TEXT,
  PRIMARY KEY (video_id, run, route));
CREATE TABLE IF NOT EXISTS channel_list (
  run TEXT, seed TEXT, seed_type TEXT, video_id TEXT, title TEXT, duration REAL, views INTEGER,
  pattern TEXT, artist TEXT, new_title TEXT, orig_text TEXT, songs TEXT, match_source TEXT,
  PRIMARY KEY (seed, video_id));
CREATE TABLE IF NOT EXISTS seed_log (run TEXT, seed TEXT PRIMARY KEY, n INTEGER, seconds REAL, run_at TEXT);
"""


def main():
    DB.parent.mkdir(exist_ok=True)
    (DB.parent / "raw").mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()
    out = sqlite3.connect(DB)
    out.executescript(SCHEMA)
    out.execute("ALTER TABLE video ADD COLUMN first_run TEXT")

    stats = {}
    for run, path, raw in SOURCES:
        src = sqlite3.connect(path)
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        v = pd.read_sql("SELECT * FROM video WHERE status='ok'", src)
        v["first_run"] = run
        have = {r[0] for r in out.execute("SELECT video_id FROM video")}
        new = v[~v.video_id.isin(have)]
        new.to_sql("video", out, if_exists="append", index=False)
        stats[run] = {"video": len(v), "new": len(new), "dup_with_earlier": len(v) - len(new)}

        if "video_query" in tables:  # 키워드 검색 회차
            vs = pd.read_sql("""SELECT vq.video_id, q.query AS route, vq.rank FROM video_query vq
                                JOIN queries q USING(query)""", src)
            vs = vs[vs.video_id.isin(v.video_id)].assign(run=run, route_type="keyword")
            vs.to_sql("video_source", out, if_exists="append", index=False)
            pd.read_sql("SELECT * FROM queries", src).assign(run=run).to_sql("queries", out, if_exists="append", index=False)
        if "channel_list" in tables:  # 채널 순회 회차
            cl = pd.read_sql("SELECT * FROM channel_list", src).assign(run=run)
            cl.to_sql("channel_list", out, if_exists="append", index=False)
            vs = cl[cl.video_id.isin(v.video_id)].rename(columns={"seed": "route", "seed_type": "route_type"})
            vs[["video_id", "run", "route_type", "route", "pattern", "songs", "match_source"]].drop_duplicates(
                ["video_id", "route"]).to_sql("video_source", out, if_exists="append", index=False)
            pd.read_sql("SELECT * FROM seed_log", src).assign(run=run).to_sql("seed_log", out, if_exists="append", index=False)
        if "subtitle" in tables:
            pd.read_sql("SELECT * FROM subtitle", src).to_sql("subtitle", out, if_exists="append", index=False)

        copied = 0
        for f in raw.glob("*.json.gz"):
            dst = DB.parent / "raw" / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
                copied += 1
        stats[run]["raw_copied"] = copied
    out.commit()

    n_video = out.execute("SELECT COUNT(*) FROM video").fetchone()[0]
    n_src = out.execute("SELECT COUNT(*) FROM video_source").fetchone()[0]
    multi = out.execute("SELECT COUNT(*) FROM (SELECT video_id FROM video_source GROUP BY video_id HAVING COUNT(DISTINCT run) > 1)").fetchone()[0]
    n_list = out.execute("SELECT COUNT(DISTINCT video_id) FROM channel_list").fetchone()[0]
    print(stats)
    print(f"video={n_video} video_source={n_src} seen_in_multiple_runs={multi} channel_list_unique={n_list} -> {DB}")


if __name__ == "__main__":
    main()
