"""목업 v2 분석: 채널 순회 수율, 제목 확정 비율, 오디오 품질 등급, AI 의심, 작품 묶기.

입력: mock_v2/mock_v2.sqlite (+ 비교용 mock/mock_videos_labeled.csv)
출력: mock_v2/mock_v2_labeled.csv, 콘솔 요약
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "process"))
from mock_analyze import MEDIA_RULES, SPECTRUM_RULES, key, nfc  # noqa: E402

V2 = ROOT / "mock_v2"
AI_RE = re.compile(r"AI\s?(도구|생성|국악|음악|보컬|목소리|커버)|인공지능|\bSuno\b|수노|\bUdio\b|AI가 (만든|부른)|generated with|made with AI|#AI", re.I)
BROADCAST_STUDIO = re.compile(r"FM\s?LIVE|오픈\s?스튜디오|라디오|스튜디오\s?라이브|Studio Live", re.I)
PHONE_CAM = re.compile(r"직캠|폰캠|fancam|휴대폰|핸드폰", re.I)


def audio_tier(r, text):
    """오디오 품질 등급: 1 공식 음원 / 2 방송 스튜디오 / 3 공식 MV·라이브 클립·방송 무대 / 4 공연 실황 / 5 직캠·재업로드."""
    if r.media_form == "topic_audio" or re.search(r"official audio|full album|음원", text, re.I):
        return 1
    if BROADCAST_STUDIO.search(text):
        return 2
    if r.media_form in ("official_mv", "broadcast") or re.search(r"live clip|라이브 클립", text, re.I):
        return 3
    if PHONE_CAM.search(text):
        return 5
    if r.seed_type == "fusion_team" or r.seed_type == "institution" or r.media_form == "live_stage":
        return 4
    return 4


def main():
    con = sqlite3.connect(V2 / "mock_v2.sqlite")
    cl = pd.read_sql("SELECT * FROM channel_list", con)
    seeds = pd.read_sql("SELECT * FROM seed_log", con)
    v = pd.read_sql("SELECT * FROM video WHERE status='ok'", con)
    p = print

    # ---- 1. 채널 순회 수율 (목록만으로) ----
    cl["matched"] = cl.songs.fillna("") != ""
    by = cl.groupby(["seed_type", "seed"]).agg(videos=("video_id", "size"), matched=("matched", "sum"),
                                               paren=("match_source", lambda x: (x == "paren").sum()))
    by["match_rate"] = (by.matched / by.videos).round(2)
    by = by.join(seeds.set_index("seed")[["seconds"]], on="seed").round(1)
    p("== channel listing yield ==")
    p(by.to_string())
    p(f"total listed={len(cl)} unique={cl.video_id.nunique()} matched={cl[cl.matched].video_id.nunique()} "
      f"listing time={seeds.seconds.sum():.0f}s")
    p("pattern counts (matched):", cl[cl.matched].pattern.value_counts().to_dict())
    # 괄호 확정을 둘로 나눔: 신곡명(원곡) vs 셋리스트(곡 여러 개 나열 → 여러 곡이 든 영상)
    pc = cl[cl.match_source == "paren"].copy()
    pc["setlist"] = pc.orig_text.str.contains(r"[,+·、]") | (pc.songs.str.count(r"\|") >= 1)
    p(f"paren total={len(pc)}: derived title(신곡명(원곡))={int((~pc.setlist).sum())}, setlist={int(pc.setlist.sum())}")
    cl["multi_song"] = cl.songs.fillna("").str.count(r"\|") >= 1
    p(f"titles naming 2+ anchor songs (multi-song videos): {int(cl.multi_song.sum())}")
    ft = cl[cl.seed_type.isin(["fusion_team", "topic"])]
    p(f"fusion-team channels: non-matched share={1 - ft.matched.mean():.0%} (민요 무관·창작곡·제목에 원곡명 없음 포함)")
    p("songs covered (unique anchor songs):", cl[cl.matched].songs.str.split("|").explode().nunique())

    # ---- 2. 상세 메타 라벨 ----
    d = v.merge(cl.drop_duplicates("video_id")[["video_id", "seed", "seed_type", "pattern", "artist", "new_title",
                                                 "orig_text", "songs", "match_source"]], on="video_id", how="left")
    rows = []
    for _, r in d.iterrows():
        tags = " ".join(json.loads(r.tags or "[]"))
        desc = nfc(r.description or "")
        text = nfc(f"{r.title} {tags} {r.channel} {desc[:800]}")
        media = "short" if r.is_short else next((m for m, f in MEDIA_RULES if f(r, text)), "other")
        spec = next((c for c, pat in SPECTRUM_RULES if re.search(pat, nfc(f"{r.title} {tags} {desc[:1500]}"), re.I)), "?")
        r2 = r.copy()
        r2["media_form"] = media
        rows.append({
            "video_id": r.video_id, "seed": r.seed, "seed_type": r.seed_type, "title": nfc(r.title),
            "songs": r.songs, "match_source": r.match_source, "pattern": r.pattern,
            "artist": r.artist, "new_title": r.new_title,
            "n_songs": len(str(r.songs).split("|")) if isinstance(r.songs, str) and r.songs else 0,
            "duration": r.duration, "views": r.view_count, "upload_year": str(r.upload_date)[:4],
            "media_form": media, "spectrum_guess": spec, "audio_tier": audio_tier(r2, text),
            "ai_suspect": bool(AI_RE.search(desc) or AI_RE.search(nfc(r.title))),
            "desc_borrow_note": bool(re.search(r"(가사|선율|가락|멜로디|후렴)를?\s*(빌려|차용|모티(브|프))|원곡|민요를?\s*(바탕|재해석|편곡)", desc)),
            "has_ko_auto": r.has_ko_auto, "url": f"https://youtu.be/{r.video_id}",
        })
    df = pd.DataFrame(rows)
    # 작품 묶기: 아티스트(없으면 채널) + 원곡 + 신곡명
    df["work_id"] = [key(f"{a if isinstance(a, str) else s}|{so}|{n if isinstance(n, str) else ''}")[:60]
                     for a, s, so, n in zip(df.artist, df.seed, df.songs, df.new_title)]
    df.to_csv(V2 / "mock_v2_labeled.csv", index=False, encoding="utf-8-sig")

    p("\n== detail (n=%d) ==" % len(df))
    p("seed_type:", df.seed_type.value_counts().to_dict())
    p("match_source:", df.match_source.value_counts().to_dict())
    p("audio_tier:\n", pd.crosstab(df.audio_tier, df.seed_type).to_string())
    p("media_form:", df.media_form.value_counts().to_dict())
    p("ai_suspect:", int(df.ai_suspect.sum()), "| desc borrow note:", int(df.desc_borrow_note.sum()))
    p("duration median by seed_type:", df.groupby("seed_type").duration.median().to_dict())
    p("long (>10min):", int((df.duration > 600).sum()), "| multi-song titles:", int((df.n_songs >= 2).sum()))
    p("views median by seed_type:", df.groupby("seed_type").views.median().to_dict())
    p("works:", df.work_id.nunique(), "videos per work max:", df.work_id.value_counts().max())
    p("upload year:", df.upload_year.value_counts().sort_index().to_dict())
    p("auto ko subs:", f"{df.has_ko_auto.mean():.0%}")
    p("top songs:", df.songs.str.split("|").explode().value_counts().head(15).to_dict())

    # ---- 3. v1(키워드)과 비교 ----
    v1 = pd.read_csv(ROOT / "mock/mock_videos_labeled.csv")
    v1_t12 = v1.media_form.isin(["topic_audio", "official_mv"]).mean()
    p("\n== v1 vs v2 ==")
    p(f"v1 keyword: official audio/MV {v1_t12:.0%}, views median {v1.views.median():.0f}")
    p(f"v2 channel: tier1-2 {(df.audio_tier <= 2).mean():.0%}, tier1-3 {(df.audio_tier <= 3).mean():.0%}, "
      f"paren-confirmed {(df.match_source == 'paren').mean():.0%}, views median {df.views.median():.0f}")
    p(f"overlap with v1 videos: {df.video_id.isin(v1.video_id).sum()}")

    p("\n== samples ==")
    pd.set_option("display.width", 230)
    pd.set_option("display.max_colwidth", 75)
    for st in ["fusion_team", "topic", "broadcast", "institution"]:
        p(f"-- {st}")
        p(df[df.seed_type == st][["title", "songs", "audio_tier", "media_form"]].head(8).to_string())
    p("-- ai suspects")
    p(df[df.ai_suspect][["title", "seed"]].to_string())


if __name__ == "__main__":
    main()
