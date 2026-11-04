---
name: opinion
description: 상표 의견제출통지서 PDF를 받아 대화로 분석 보고서, 대응 전략 추천과 선택, 의견서 초안, 법리와 판례 근거 목록, 지정상품 삭제 보정서 초안을 작성한다. Use when a practitioner gives a Korean trademark office action notice (의견제출통지서) or asks for 의견서 or 보정서 drafting with the OA MCP server.
---

# OA 의견서 (opinion)

이 스킬은 OA MCP 서버(`oa`)가 있어야 동작한다. 작성 절차와 참고 문서는 로그인한 사용자에게만 서버가 준다.

1. 먼저 `get_guide(skill="opinion")`를 부르고, 받은 절차를 처음부터 끝까지 그대로 따른다. 절차가 참고 문서를 읽으라고 하면 그때 `get_guide(topic="...")`로 받는다.
2. 실무자에게 묻는 것은 반드시 Codex의 질문 도구(`request_user_input`, 그 도구가 없을 때만 `request_user_input_async`)로만 묻는다. 본문 답변으로 질문하지 않는다.
3. 응답의 `guide_version`을 기억해 두었다가, 피드백을 보낼 때 `repro.guide_version`에 그대로 넣는다.
4. 절차에서 말하는 `<이 스킬 폴더>`는 이 `SKILL.md`가 있는 폴더다. 통지서 PDF 글자는 이 폴더의 `scripts/notice_text.py`로, 통지서 "상표견본이미지" 표의 본원과 인용 견본 그림은 `scripts/notice_specimens.py`로 로컬에서 뽑는다(둘 다 네트워크를 쓰지 않는다).
5. 네이버 검색 화면 증거는 이 플러그인의 로컬 도구 `oa_capture`의 `naver_capture`(`application_number`, `query`, `out_dir`=<작업 폴더 절대 경로>/web-captures, `channel`)로 실무자 PC에서 캡처한다(사건당 최대 6회, 3초 간격, 검색어만 네이버로 감). 이 도구는 코덱스 명령 샌드박스 밖에서 돌므로, 같은 일을 하는 `scripts/naver_capture.py`를 셸 명령으로 실행하지 않는다(샌드박스 안에서는 네트워크가 막히고 Chrome이 시작하지 못한다). 돌려받은 기록은 서버 도구 register_capture에 그대로 넘겨 조사 카드로 받는다.
6. 통지서 PDF는 서버에 올리지 않는다. 어떤 도구가 `not_configured`를 주어도 실무자에게 서버 설정을 묻지 않고 절차대로 계속한다.
7. 의견서 본문은 서버 절차에서 검사를 통과한 변론 계획대로만 쓰고, 불리한 해석과 위험 검토는 `report.md`에만 남긴다.
8. `opinion.md`는 서버 도구 finalize_opinion이 발급한 `markdown`만 그대로 쓰고, 옆에 발급 기록(`sidecar`)을 함께 쓴다. 발급받지 못하면 `opinion.md`를 쓰지 않고 `report.md`에 남은 항목을 적어 알린다.
9. `oa` 도구가 보이지 않거나 `get_guide`가 `access_denied`를 주면, 실무자에게 터미널에서 `codex mcp login oa`를 실행해 허용 목록에 있는 Google 계정으로 로그인해 달라고 알리고 멈춘다. 절차를 짐작해서 대신 쓰지 않는다.
