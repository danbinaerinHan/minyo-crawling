"""통합 DB(db/minyo.sqlite)에서 수집 현황 페이지(review/수집현황.html)를 만든다.

자동 라벨(영상 형태, 갈래, 오디오 등급, AI 의심)은 mock_analyze*의 규칙을 그대로 쓴다.
영상이 여러 경로로 잡혔으면 대표 경로를 하나 고른다: 퓨전팀 > 방송 > 기관 > 키워드.

사용: .venv/bin/python process/build_status_page.py
"""
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "process"))
from mock_analyze import MEDIA_RULES, SPECTRUM_RULES, nfc  # noqa: E402
from mock_analyze_v2 import AI_RE, audio_tier  # noqa: E402

DB = ROOT / "db" / "minyo.sqlite"
REV = ROOT / "review"
ROUTE_PRIO = {"fusion_team": 0, "topic": 0, "snow3": 0, "snow4": 1, "broadcast": 1, "institution": 2, "shorts": 2.5,
              "keyword": 3, "keyword_rare": 3, "artist2": 3, "fusion_q": 3, "rare2": 3, "pop": 3, "show_q": 3}
# 페이지의 4개 경로 분류로 묶기. 검색 계열은 키워드, 눈덩이 채널은 퓨전팀.
# 숏츠는 채널 탭에서 받은 것이라 같은 채널의 다른 영상 분류를 따른다 (아래 main).
ROUTE_GROUP = {"topic": "fusion_team", "snow3": "fusion_team", "snow4": "broadcast", "keyword_rare": "keyword", "artist2": "keyword",
               "fusion_q": "keyword", "rare2": "keyword", "pop": "keyword", "show_q": "keyword"}
# 제목상 여러 곡인데 곡 목록이 없는 영상 (메들리·연곡·모음·몰아듣기·풀버전)
MULTI_HINT = re.compile(r"모음|메들리|medley|연곡|몰아\s?듣기|\d+\s?곡|외\s*(?:메들리|\d|$|[\]\)|/])|full|풀버전|전곡", re.I)
SEARCH_TYPES = {"keyword_rare", "artist2", "fusion_q", "rare2", "shorts", "pop", "show_q"}


def main():
    con = sqlite3.connect(DB)
    v = pd.read_sql("SELECT * FROM video WHERE status='ok'", con)
    v[["channel", "description", "title"]] = v[["channel", "description", "title"]].fillna("")  # 채널명 없는 영상 대비
    vs = pd.read_sql("SELECT * FROM video_source", con)
    vs["prio"] = vs.route_type.map(ROUTE_PRIO).fillna(9)
    best = vs.sort_values(["prio", "run"]).drop_duplicates("video_id").set_index("video_id")
    songs_by_vid = (vs.assign(s=vs.songs.fillna("").str.split("|")).explode("s")
                    .query("s != ''").groupby("video_id").s.apply(lambda x: sorted(set(x))))
    paren = set(vs.loc[vs.match_source == "paren", "video_id"])
    # 기타 민요: 곡명 사전에는 없지만 제목으로 민요로 보이는 영상 (후보 곡명)
    gl = pd.read_sql("SELECT video_id, orig_text FROM channel_list WHERE match_source='genre'", con)
    etc = gl.drop_duplicates("video_id").set_index("video_id").orig_text.to_dict()

    def shape(vid):
        """raw JSON의 대표 해상도로 세로 영상인지 본다 (숏츠 여부는 채널 /shorts 탭으로 따로 확정)."""
        f = DB.parent / "raw" / f"{vid}.json.gz"
        try:
            d = json.load(gzip.open(f))
            return (d.get("height") or 0) > (d.get("width") or 0) > 0
        except (OSError, ValueError):
            return False

    tl = pd.read_sql("SELECT * FROM video_text_labels", con).set_index("video_id")  # process/desc_mining.py
    sl = pd.read_sql("SELECT video_id, t_sec, label, songs FROM desc_setlist WHERE songs != ''", con)
    # 자막 가사로 찾은 곡 (process/subs_borrow.py)
    has_ss = con.execute("SELECT 1 FROM sqlite_master WHERE name='subtitle_songs'").fetchone()
    lyr = (pd.read_sql("SELECT video_id, song FROM subtitle_songs ORDER BY hits DESC", con)
           .groupby("video_id").song.apply(lambda x: ", ".join(x[:5])).to_dict() if has_ss else {})
    setlist = {vid: g.sort_values("t_sec")[["t_sec", "songs"]].values.tolist() for vid, g in sl.groupby("video_id")}

    rows = []
    for i, r in enumerate(v.sort_values("crawled_at").itertuples(), 1):
        b = best.loc[r.video_id] if r.video_id in best.index else None
        rtype = ROUTE_GROUP.get(b.route_type, b.route_type) if b is not None else "keyword"
        tags = " ".join(json.loads(r.tags or "[]"))
        desc = nfc(r.description or "")
        text = nfc(f"{r.title} {tags} {r.channel} {desc[:800]}")
        dur = r.duration or 0
        vertical = shape(r.video_id)
        if vertical:
            media = "vertical"  # 세로 영상 (숏츠 가능성)
        else:
            media = next((m for m, f in MEDIA_RULES if f(r, text)), "other")
            if media == "other" and 0 < dur <= 60:
                media = "clip"  # 60초 이하 가로 영상 (예고, 강습 조각)
        spec = next((c for c, pat in SPECTRUM_RULES if re.search(pat, nfc(f"{r.title} {tags} {desc[:1500]}"), re.I)), "?")
        tier_row = pd.Series({"media_form": media, "seed_type": rtype})
        songs = list(songs_by_vid.get(r.video_id, []))
        lab = tl.loc[r.video_id] if r.video_id in tl.index else None
        if lab is not None and lab.title_songs:  # 제목 재파싱으로 보완
            songs = sorted(set(songs) | set(lab.title_songs.split("|")))
        dsongs = lab.desc_songs.split("|") if lab is not None and lab.desc_songs else []
        cue = lab.desc_cue if lab is not None else ""
        rows.append({
            "n": i, "id": r.video_id, "v": (b.run if b is not None else r.first_run) or "",
            "route": (b.route if b is not None else "") or "", "ch": nfc(r.channel), "t": nfc(r.title),
            "dur": int(dur) if dur else None, "views": None if pd.isna(r.view_count) else int(r.view_count),
            "yr": str(r.upload_date or "")[:4], "songs": songs, "src": "paren" if r.video_id in paren else ("title" if songs else ""),
            "pat": "", "spec": spec, "form": media, "tier": audio_tier(tier_row, text),
            "ai": bool(AI_RE.search(desc) or AI_RE.search(nfc(r.title))), "multi": len(songs) >= 2 or bool(MULTI_HINT.search(nfc(r.title))),
            "mh": len(songs) < 2 and bool(MULTI_HINT.search(nfc(r.title))),
            "sub": "auto" if r.has_ko_auto else "none", "lyr": lyr.get(r.video_id, ""), "st": rtype,
            "etc": "" if songs else (etc.get(r.video_id) or ""),
            "dsongs": dsongs, "cue": cue[:120],
            "pop": (lab.pop_songs.split("|") if lab is not None and lab.pop_songs else []),
            "pg": (lab.pop_genre if lab is not None else "") or "",
            "segs": [[int(t), so] for t, so in setlist.get(r.video_id, [])][:12],
        })

    ch_type = {}
    for r in rows:
        if r["st"] != "shorts":
            ch_type.setdefault(r["ch"], {}).setdefault(r["st"], 0)
            ch_type[r["ch"]][r["st"]] += 1
    for r in rows:
        if r["st"] == "shorts":
            c = ch_type.get(r["ch"])
            r["st"] = max(c, key=c.get) if c else "fusion_team"

    cl = pd.read_sql("SELECT seed, seed_type, video_id, songs, match_source FROM channel_list", con)
    seeds = pd.read_sql("SELECT seed, seconds FROM seed_log", con).drop_duplicates("seed").set_index("seed")
    cl = cl[~cl.seed_type.isin(SEARCH_TYPES)]
    cl["seed_type"] = cl.seed_type.replace({"snow3": "fusion_team", "snow4": "broadcast"})
    cl["m"] = cl.songs.fillna("") != ""
    have = set(v.video_id)
    yld, seen = [], {}
    for (stype, seed), g in cl.groupby(["seed_type", "seed"]):
        # 같은 채널이 두 시드 이름으로 등록된 경우(「퓨전국악 퀸」/「퓨전국악그룹 퀸」) 한 줄로 합친다
        sig = frozenset(g.video_id)
        if sig in seen:
            seen[sig]["seed"] += f" ({seed})"
            continue
        yld.append({"type": stype, "seed": seed, "videos": len(g), "matched": int(g.m.sum()),
                    "paren": int((g.match_source == "paren").sum()), "detailed": int(g.video_id.isin(have).sum()),
                    "sec": round(float(seeds.loc[seed, "seconds"]), 1) if seed in seeds.index else None})
        seen[sig] = yld[-1]

    seg_path = ROOT / "mock/long/ftcCjBbMJHY.segments.csv"
    segs = pd.read_csv(seg_path).to_dict("records") if seg_path.exists() else []
    runs = v.first_run.value_counts().to_dict()
    data = {"rows": rows, "yield": yld, "segments": segs, "longVideo": "ftcCjBbMJHY",
            "built": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"), "runs": runs,
            "rare": int((vs.route_type == "keyword_rare").sum())}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tpl = (REV / "수집현황_template.html").read_text(encoding="utf-8")
    (REV / "수집현황.html").write_text(tpl.replace("/*__DATA__*/null", blob), encoding="utf-8")
    print(f"rows={len(rows)} runs={runs}")


if __name__ == "__main__":
    main()
