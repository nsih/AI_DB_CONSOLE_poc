# DB Managing Console

로컬 LLM 기반 NL2SQL DB 관리 콘솔 (Streamlit + MySQL)

## 기능

- 테이블 조회
- PDF / CSV / Excel 파싱 → 확인·수정 → 테이블 생성 및 적재
- 자연어(또는 직접 입력) → 쿼리 생성 → 확인·수정 → 실행

## 설치 및 실행

```bash
# 1. MySQL 기동 (.env는 .env.example 참고해서 생성)
docker compose -f docker-compose.mysql.yml up -d

# 2. Python 환경
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Streamlit secrets 생성 (.streamlit/secrets.toml.example 참고)
#    DB_PASSWORD는 .env의 MYSQL_USER_PASSWORD와 동일해야 함

# 4. 실행
streamlit run db_app.py
```

SQL 생성에는 LM Studio 등에서 띄운 OpenAI 호환 서버가 필요하다. 모델을 로드한 뒤 해당 주소를 secrets의 `AI_WORKER_IP`에 지정한다.

> 앱 자체에 인증이 없다. 신뢰되지 않은 네트워크에 노출할 경우 리버스 프록시나 방화벽으로 접근을 제한할 것.

## 테스트

```bash
pip install -r requirements-dev.txt
pytest
```

## 검증 환경

| 구분 | 사양 |
| --- | --- |
| 앱 서버 (VM) | Xeon E5-2630 v4 / RAM 4GB |
| LLM 호스트 | Core Ultra 7 265 / RAM 16GB / LM Studio + Qwen3-4B |

패키지 버전은 `requirements.txt` 참고.


 ## comment
아직도 돈과 데이터를 넘기며 상용 AI를 쓰십니까?

이제 자연어로 무한쿼리를 생성하며 직접 DB를 관리하세요