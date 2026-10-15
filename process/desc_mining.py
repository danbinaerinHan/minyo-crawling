"""설명란 분석 (YouTube 요청 없음): 통합 DB video.description에서
  1) title_songs   제목을 최신 곡명 사전으로 다시 파싱한 곡 (경로 기록에 곡이 없던 영상 보완)
  2) desc_songs    설명란에 언급된 곡 (곡명 사전 기준)
  3) desc_cue      차용 단서 문구 (원곡, 민요, 가사를 빌려, 모티브, 설화 …)
  4) desc_setlist  설명란 타임스탬프 셋리스트 (00:00 곡명) → 긴 영상 구간 나누기용

  5) pop_songs/pop_genre  민요에서 나온 대중가요·트로트 (master/pop_derived.csv), 제목의 국악가요·트로트

출력 테이블: video_text_labels(video_id, title_songs, desc_songs, desc_cue, pop_songs, pop_genre), desc_setlist(video_id, t_sec, label, songs)
사용: .venv/bin/python process/desc_mining.py
"""
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawl"))
from mock_crawl_v2 import build_name_dict, build_pop_dict, find_pop, find_songs, parse_title  # noqa: E402

DB = ROOT / "db" / "minyo.sqlite"
CUE = re.compile(r"원곡|민요|전래|가사를?\s*(빌|차용|가져)|선율을?\s*(빌|차용|가져)|가락을?\s*(빌|차용)|모티(브|프)|재해석|재구성|편곡|설화|토리|후렴")
TS = re.compile(r"(?m)^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?\s*[-–:|.]?\s*(.+)$|((?:\d{1,2}:)?\d{1,2}:\d{2})\s+([^\n0-9][^\n]{1,60})")


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def to_sec(t):
    parts = [int(x) for x in t.split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def main():
    names = build_name_dict()
    pop = build_pop_dict()
    con = sqlite3.connect(DB)
    v = pd.read_sql("SELECT video_id, title, description, duration FROM video WHERE status='ok'", con)
    labels, setlist = [], []
    for r in v.itertuples():
        title, desc = nfc(r.title), nfc(r.description)
        t_songs = parse_title(title, names)[4]
        d_songs = [s for s in find_songs(desc, names) if s not in t_songs]
        m = CUE.search(desc)
        cue = desc[max(0, m.start() - 30): m.end() + 50].replace("\n", " ") if m else ""
        p_songs, p_genre = find_pop(title, pop)
        labels.append((r.video_id, "|".join(t_songs), "|".join(d_songs), cue, "|".join(p_songs), p_genre))
        for mm in TS.finditer(desc):
            t, label = (mm.group(1), mm.group(2)) if mm.group(1) else (mm.group(3), mm.group(4))
            sec = to_sec(t)
            if r.duration and sec > r.duration + 5:
                continue
            label = label.strip()[:80]
            setlist.append((r.video_id, sec, label, "|".join(find_songs(label, names))))
    con.execute("DROP TABLE IF EXISTS video_text_labels")
    con.execute("CREATE TABLE video_text_labels (video_id TEXT PRIMARY KEY, title_songs TEXT, desc_songs TEXT, desc_cue TEXT, "
                "pop_songs TEXT, pop_genre TEXT)")
    con.executemany("INSERT INTO video_text_labels VALUES (?,?,?,?,?,?)", labels)
    con.execute("DROP TABLE IF EXISTS desc_setlist")
    con.execute("CREATE TABLE desc_setlist (video_id TEXT, t_sec INTEGER, label TEXT, songs TEXT)")
    con.executemany("INSERT INTO desc_setlist VALUES (?,?,?,?)", setlist)
    con.commit()
    sl = pd.DataFrame(setlist, columns=["video_id", "t", "label", "songs"])
    vids_with_song_ts = sl[sl.songs != ""].video_id.nunique()
    lab = pd.DataFrame(labels, columns=["id", "ts", "ds", "cue", "ps", "pg"])
    print(f"videos={len(v)} title_songs={int((lab.ts != '').sum())} desc_songs_extra={int((lab.ds != '').sum())} "
          f"cue={int((lab.cue != '').sum())} pop={int((lab.ps != '').sum())} pop_genre={int((lab.pg != '').sum())} | setlist rows={len(sl)} videos_with_timestamped_songs={vids_with_song_ts}")


if __name__ == "__main__":
    main()
