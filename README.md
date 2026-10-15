# minyo-crawling

유튜브에서 한국 민요(통속민요·토속민요)와 민요를 차용한 퓨전국악·대중가요 영상의 링크와 메타데이터를 모으는 크롤러.
영상 파일은 받지 않고 video_id·제목·채널·설명·자막만 SQLite(`db/minyo.sqlite`, 저장소에는 미포함)에 쌓는다.

- `crawl/` — yt-dlp 기반 수집 (채널 목록 훑기, 키워드 검색, 상세 정보, 자막). 요청 제한이 오면 15분부터 두 배씩 쉰다.
- `process/` — 제목·설명·자막에서 곡 매칭, 숨은 차용 탐지, 모니터링 페이지 생성
- `master/` — 곡 이름 사전, 별칭, 민요 기반 대중가요 목록
- `config/` — 수집 대상 채널 목록
- `review/수집현황_template.html` — 모니터링 페이지 템플릿
- `COLLECTION_LOG_v3_밤샘수집.md` — 수집 과정 기록

곡 사전의 기준(anchor)은 GugakVocalSet의 `total_metadata.csv`이며, 이 파일과 여기서 뽑은 곡 사전·가사 구절(`master/anchor_songs.csv`, `master/anchor_phrases.csv`)은 저장소에 포함하지 않는다.
