"""수집 현황 페이지(review/수집현황.html) 생성: 템플릿에 목업 v1·v2 데이터를 JSON으로 넣는다.

입력: review/목업_검수목록.csv (export_review.py), mock_v2/mock_v2.sqlite, mock_v2/mock_v2_labeled.csv
템플릿: review/수집현황_template.html 의 /*__DATA__*/ 자리
사용: .venv/bin/python process/build_review_page.py
"""
import json
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
REV = ROOT / "review"


def seed_types():
    v2 = pd.read_csv(ROOT / "mock_v2/mock_v2_labeled.csv")
    return dict(zip(v2.video_id, v2.seed_type))


def main():
    df = pd.read_csv(REV / "목업_검수목록.csv", dtype={"업로드연도": str})
    seed_type = seed_types()

    def s(x):
        return "" if pd.isna(x) else str(x)

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "n": int(r.번호), "id": r.video_id, "v": r.차수, "route": s(r.수집경로), "ch": s(r.채널),
            "t": s(r.제목), "dur": None if pd.isna(r["길이(초)"]) else int(r["길이(초)"]),
            "views": None if pd.isna(r.조회수) else int(r.조회수), "yr": s(r.업로드연도),
            "songs": [x for x in s(r.매칭곡).split("|") if x], "src": s(r.매칭근거), "pat": s(r.제목패턴),
            "spec": s(r.갈래추정), "form": s(r.영상형태),
            "tier": None if s(r.오디오등급) == "" else int(float(r.오디오등급)),
            "ai": s(r.AI의심) == "의심", "multi": bool(r.여러곡), "sub": s(r.자동자막), "lyr": s(r["가사구절(v1)"]),
            "st": seed_type.get(r.video_id, "keyword"),
        })

    con = sqlite3.connect(ROOT / "mock_v2/mock_v2.sqlite")
    cl = pd.read_sql("SELECT * FROM channel_list", con)
    seeds = pd.read_sql("SELECT * FROM seed_log", con).set_index("seed")
    cl["m"] = cl.songs.fillna("") != ""
    yld = []
    for (stype, seed), g in cl.groupby(["seed_type", "seed"]):
        yld.append({"type": stype, "seed": seed, "videos": len(g), "matched": int(g.m.sum()),
                    "paren": int((g.match_source == "paren").sum()),
                    "sec": round(float(seeds.loc[seed, "seconds"]), 1) if seed in seeds.index else None})

    seg_path = ROOT / "mock/long/ftcCjBbMJHY.segments.csv"
    segs = pd.read_csv(seg_path).to_dict("records") if seg_path.exists() else []

    data = {"rows": rows, "yield": yld, "segments": segs, "longVideo": "ftcCjBbMJHY",
            "built": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    tpl = (REV / "수집현황_template.html").read_text(encoding="utf-8")
    out = tpl.replace("/*__DATA__*/null", blob)
    (REV / "수집현황.html").write_text(out, encoding="utf-8")
    print(f"rows={len(rows)} size={len(out) / 1024:.0f}KB")


if __name__ == "__main__":
    main()
