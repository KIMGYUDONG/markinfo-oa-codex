# 마크인포 OA 코덱스 플러그인

마크인포 상표 OA(의견제출통지 대응) 업무를 코덱스(Codex)에서 하는 플러그인입니다. 첫 스킬 `opinion`은 의견제출통지서 PDF로 의견서 초안 작업을 돕습니다. 작성 방법은 OA 서버에 로그인한 사용자에게만 제공되며, 이 저장소에는 플러그인 설치 파일만 있습니다.

## 설치

터미널에서 차례로 실행합니다.

```sh
codex plugin marketplace add KIMGYUDONG/markinfo-oa-codex
codex plugin add oa@markinfo-oa
codex mcp login oa
```

마지막 명령은 브라우저를 열어 Google 로그인을 요청합니다. 마크인포에서 허용한 Google 계정으로 로그인하세요. 허용되지 않은 계정은 도구를 쓸 수 없습니다.

## 사용

코덱스에서 "이 의견제출통지서로 의견서 초안을 만들어 주세요: <통지서.pdf 경로>"처럼 요청하면 `opinion` 스킬이 시작됩니다. 도구가 보이지 않는다는 안내가 나오면 `codex mcp login oa`로 다시 로그인하세요.

## 업데이트

```sh
codex plugin marketplace upgrade markinfo-oa
```

## 삭제

```sh
codex plugin remove oa@markinfo-oa
```

## 문의와 권리

마크인포 내부 업무용입니다. 사용 문의는 마크인포 담당자에게 해 주세요. 권리 고지는 `LICENSE`를 보세요.
