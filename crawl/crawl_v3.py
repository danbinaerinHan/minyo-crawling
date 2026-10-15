"""v3 수집기: 통합 DB(db/minyo.sqlite)에 바로 쌓는다.

  rematch   channel_list 전체를 최신 곡명 사전(anchor + folk_song_extra)으로 다시 매칭 (요청 없음)
  coverage  곡별 커버리지 요약 (요청 없음)
  detail    매칭됐지만 상세가 없는 영상을 받는다. --types 로 시드 유형 제한, --per-seed-cap 으로 채널당 상한

사용 예:
  .venv/bin/python crawl/crawl_v3.py rematch
  .venv/bin/python crawl/crawl_v3.py coverage
  .venv/bin/python crawl/crawl_v3.py detail --types broadcast,institution --run v3_broadcast
"""
import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
sys.path.insert(0, str(ROOT / "process"))
import mock_crawl  # noqa: E402
from mock_crawl_v2 import build_name_dict, parse_title  # noqa: E402

DB = ROOT / "db" / "minyo.sqlite"


def rematch(con):
    names = build_name_dict()
    cl = pd.read_sql("SELECT rowid, seed, video_id, title FROM channel_list", con)
    before = con.execute("SELECT COUNT(*) FROM channel_list WHERE songs != ''").fetchone()[0]
    rows = []
    for rid, title in zip(cl.rowid, cl.title):
        p = parse_title(title or "", names)
        rows.append((p[0], p[1], p[2], p[3], "|".join(p[4]), p[5], rid))
    con.executemany("""UPDATE channel_list SET pattern=?, artist=?, new_title=?, orig_text=?, songs=?, match_source=?
                       WHERE rowid=?""", rows)
    # 이미 상세를 받은 영상의 경로 기록(video_source)도 새 매칭으로 맞춘다
    con.execute("""UPDATE video_source SET pattern=c.pattern, songs=c.songs, match_source=c.match_source
                   FROM channel_list c WHERE c.video_id=video_source.video_id AND c.seed=video_source.route""")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM channel_list WHERE songs != ''").fetchone()[0]
    mock_crawl.log(f"rematch: matched {before} -> {after} (names={len(names)})")


def candidates(con):
    """기타 민요로 잡힌 후보 곡명 빈도표 → master/candidate_new_songs.csv"""
    g = pd.read_sql("SELECT seed, video_id, title, orig_text FROM channel_list WHERE match_source='genre'", con)
    g = g.drop_duplicates("video_id")
    ex = g.assign(cand=g.orig_text.str.split("|")).explode("cand")
    out = (ex.groupby("cand").agg(videos=("video_id", "nunique"), seeds=("seed", lambda x: ", ".join(sorted(set(x))[:4])),
                                  example=("title", "first")).sort_values("videos", ascending=False).reset_index())
    out.to_csv(ROOT / "master/candidate_new_songs.csv", index=False, encoding="utf-8-sig")
    mock_crawl.log(f"genre-tagged videos={len(g)}, candidate names={len(out)} -> master/candidate_new_songs.csv")


def coverage(con):
    songs = pd.read_csv(ROOT / "master/anchor_songs.csv")
    extra = pd.read_csv(ROOT / "master/folk_song_extra.csv")
    universe = set(songs.loc[songs.tier <= 2, "title_norm"].str.replace(r"\s*\d+$", "", regex=True)) | set(extra.maps_to)
    tier1 = set(songs.loc[songs.tier == 1, "title_norm"].str.replace(r"\s*\d+$", "", regex=True)) | set(extra.loc[extra.tier == 1, "maps_to"])
    cl = pd.read_sql("SELECT seed_type, video_id, songs FROM channel_list WHERE songs != ''", con)
    kw = pd.read_sql("SELECT video_id, songs FROM video_source WHERE route_type='keyword'", con)
    seen = pd.concat([cl[["video_id", "songs"]], kw]).dropna()
    per = seen.assign(song=seen.songs.str.split("|")).explode("song").drop_duplicates(["video_id", "song"]).song.value_counts()
    covered = set(per.index) & universe
    print(f"universe(tier1-2 + extra)={len(universe)} (tier1 {len(tier1)})")
    print(f"covered in listings={len(covered)}  tier1 covered={len(covered & tier1)}/{len(tier1)}")
    for lo, hi in [(1, 4), (5, 19), (20, 99), (100, 10**9)]:
        print(f"  songs with {lo}-{hi if hi < 10**9 else '+'} videos: {int(((per >= lo) & (per <= hi)).sum())}")
    missing = sorted(universe - covered)
    print(f"not seen ({len(missing)}): tier1 = {sorted(set(missing) & tier1)}")
    print(f"  others = {sorted(set(missing) - tier1)}")
    print("top:", per.head(20).to_dict())


def detail(con, types, cap, run, limit, include_title=None, seeds=None, all_in_seeds=False, include_only=False):
    # 성공한 영상만 건너뛴다. 실패(차단 중 'Video unavailable' 포함)는 다시 시도
    con.execute("DELETE FROM video WHERE status != 'ok'")
    con.commit()
    have = {r[0] for r in con.execute("SELECT video_id FROM video")}
    cl = pd.read_sql("SELECT seed, seed_type, video_id, title, pattern, songs, match_source FROM channel_list", con)
    keep = (cl.songs.fillna("") != "") | (cl.match_source == "genre")  # 기타 민요 포함
    if include_title:  # 제목에 곡명이 없어도 받을 영상 (예: 21C 한국음악프로젝트 창작곡)
        tt = cl.title.fillna("")
        inc = tt.str.contains(include_title, regex=True) | tt.str.replace(r"\s", "", regex=True).str.contains(include_title, regex=True)
        keep = inc if include_only else (keep | inc)
    if seeds:  # 특정 시드만. all_in_seeds면 곡명 매칭과 상관없이 그 채널 영상 전부
        in_seed = cl.seed.isin(seeds)
        keep = in_seed if all_in_seeds else (keep & in_seed)
    type_ok = cl.seed_type.isin(types) if types != ["all"] else True
    cl = cl[keep & type_ok & ~cl.video_id.isin(have)].drop_duplicates("video_id")
    # 괄호 확정 먼저, 그다음 채널별로 고르게 섞어서 (중간에 멈춰도 치우치지 않게)
    cl["prio"] = (cl.match_source != "paren").astype(int)
    cl["k"] = cl.groupby("seed").cumcount()
    if cap:
        cl = cl[cl.k < cap]
    cl = cl.sort_values(["prio", "k"])
    if limit:
        cl = cl.head(limit)
    ids = cl.video_id.tolist()
    mock_crawl.log(f"detail[{run}]: {len(ids)} to fetch from {cl.seed.nunique()} seeds {cl.seed.value_counts().to_dict()}")
    if not ids:
        return
    mock_crawl.OUT = DB.parent  # raw JSON -> db/raw
    t0 = time.time()
    mock_crawl.meta_phase(con, ids)
    con.execute("UPDATE video SET first_run=? WHERE first_run IS NULL", (run,))
    src = cl[cl.video_id.isin([r[0] for r in con.execute("SELECT video_id FROM video WHERE status='ok'")])]
    con.executemany("""INSERT OR IGNORE INTO video_source (video_id, run, route_type, route, pattern, songs, match_source)
                       VALUES (?,?,?,?,?,?,?)""",
                    [(r.video_id, run, r.seed_type, r.seed, r.pattern, r.songs, r.match_source) for r in src.itertuples()])
    con.commit()
    mock_crawl.log(f"detail[{run}] done in {time.time() - t0:.0f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["rematch", "coverage", "detail", "candidates"])
    ap.add_argument("--types", default="broadcast,institution")
    ap.add_argument("--per-seed-cap", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--run", default="v3")
    ap.add_argument("--include-title", default=None, help="곡명 매칭이 없어도 받을 제목 정규식")
    ap.add_argument("--seeds", default=None, help="이 시드(쉼표 구분)만")
    ap.add_argument("--all-in-seeds", action="store_true", help="--seeds 채널의 영상 전부 (곡명 매칭 무시)")
    ap.add_argument("--include-only", action="store_true", help="--include-title에 맞는 제목만 (곡명 매칭 무시)")
    ap.add_argument("--watchlist", action="store_true", help="config/watchlist_artists.csv 이름이 제목에 있으면 포함")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    if a.mode == "rematch":
        rematch(con)
    elif a.mode == "coverage":
        coverage(con)
    elif a.mode == "candidates":
        candidates(con)
    else:
        inc = a.include_title
        if a.watchlist:
            w = pd.read_csv(ROOT / "config/watchlist_artists.csv")
            pat = "|".join(re.escape(x) for al in w.aliases for x in str(al).split("|") if len(x) >= 2)
            inc = f"{inc}|{pat}" if inc else pat
        seeds = a.seeds.split(",") if a.seeds else None
        detail(con, a.types.split(","), a.per_seed_cap, a.run, a.limit, inc, seeds, a.all_in_seeds, a.include_only)


if __name__ == "__main__":
    main()
