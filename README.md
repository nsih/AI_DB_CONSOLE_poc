# DB Managing Console
> Local LLM + NL2SQL


## 기능

0. Table 조회
1. PDF,CSV,EXEL 파싱 → 확인 및 수정 → Table 생성 및 적재
2. 자연어 → 쿼리 생성 → 확인 및 수정 → 실행

## 설치 및 실행

### 1. MySQL 기동 (Docker)

`.env` 파일 생성 (`.env.example` 참고):

```
MYSQL_ROOT_PASSWORD=<root 계정 비밀번호>
MYSQL_USER_PASSWORD=<csu_admin 계정 비밀번호>
```

```bash
docker compose -f docker-compose.mysql.yml up -d
```

### 2. Python 환경

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Streamlit secrets

`.streamlit/secrets.toml` 파일 생성 (`.streamlit/secrets.toml.example` 참고). `DB_PASSWORD`는 위 `.env`의 `MYSQL_USER_PASSWORD`와 동일해야 한다.

LM Studio에서 OpenAI 호환 서버를 켜고 모델(Qwen3-4B 등)을 로드해야 `AI_WORKER_IP`로 SQL 생성 요청이 정상 동작한다.

### 4. 실행

```bash
streamlit run db_app.py
```

기본 설정상 앱 자체에 별도 인증이 없다. 사내망 등 신뢰되지 않은 네트워크에 노출할 경우 리버스 프록시나 방화벽으로 접근을 제한할 것.

## 테스트 환경

앱 서버 (VM)
CPU : Intel Xeon E5-2630 v4 (Broadwell, AVX2 지원)
RAM : 4GB

LLM 호스트
CPU : Intel Core Ultra 7 265
RAM : 16GB
ETC : LM Studio, OpenAI 호환 API
AI : Qwen3-4B

나머지는 requirements 참고

## 

아직도 돈과 데이터를 기업에 넘기면서 상용 AI를 쓰십니까

이젠 로컬 AI로 안전하게 무한으로 쿼리 생성하고, 직접 DB를 관리하세요
