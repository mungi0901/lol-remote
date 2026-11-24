# LoL Remote

로컬 League Client(LCU)와 FastAPI, Cloudflare 임시 터널을 연동하여 모바일 브라우저에서 로비·큐·픽창·인게임 상태를 확인하고 제어할 수 있는 웹 리모컨입니다.

## 주요 기능
- **로비 및 매치메이킹:** 일반/랭크/칼바람/아수라장/아레나 로비 생성, 포지션 선택, 큐 시작·취소, 자동 수락 토글, 로비 초대 수락/거절
- **사설 방 관리:** 블루팀(`TEAM1`), 레드팀(`TEAM2`), 관전(`SPECTATOR`) 이동
- **챔피언 선택 및 아수라장:** 픽/밴, 아수라장/칼바람 선택 카드 및 벤치 교체, 픽 순서·포지션·챔피언 교환 수락/거절, 보유 스킨 선택, 픽창 채팅(퀵 메시지 포함)
- **로딩 및 인게임:** 블루/레드 팀 구성원 요약 조회 및 OP.GG 멀티서치 링크 이동

## 빠른 실행 (.exe)
1. PC에서 리그 오브 레전드 클라이언트를 실행합니다.
2. [Releases](../../releases/latest)에서 `LoL-Remote.exe`를 다운로드해 실행합니다.
3. Windows 보안 경고(SmartScreen)가 뜨면 **추가 정보 → 실행**을 클릭합니다.
4. 도메인 DNS 확인 후 터미널에 출력되는 QR 코드를 스마트폰 카메라로 스캔해 접속합니다.

## 직접 실행 및 빌드

    pip install fastapi uvicorn httpx psutil qrcode pydantic
    python server.py

    pyinstaller --noconfirm --onefile --name "LoL-Remote" --add-data "index.html;." --add-binary "cloudflared.exe;." server.py

## 주의사항 및 면책
- 본 프로젝트는 라이엇 게임즈의 공식 소프트웨어가 아니며, 개인 학습 및 편의 목적으로 제작되었습니다.
- 각 사용자 PC에서 실행 중인 클라이언트 세션을 제어하는 구조이므로, 여러 명이 동시에 한 세션을 조작하지 않도록 주의하세요.
- 비인가 클라이언트 자동화 및 서드파티 툴 사용에 따른 계정·정책 리스크는 사용자 본인 책임입니다.
