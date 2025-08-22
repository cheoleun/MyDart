from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import requests
import zipfile
import xml.etree.ElementTree as ET
import os
import json
import time
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
import pandas as pd

# 스크립트가 실행되는 디렉터리를 기준으로 .env 파일의 절대 경로를 찾습니다.
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    print("경고: .env 파일을 찾을 수 없습니다. 시스템 환경 변수를 사용합니다.")

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# --- DART API 및 기본 정보 ---
API_KEY = os.getenv("DART_API_KEY")
if not API_KEY:
    raise ValueError("DART_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")

BASE_URL = "https://opendart.fss.or.kr/api"
CORP_CODE_FILENAME = "CORPCODE.xml"
REPORT_CODES = {"Q1": "11013", "Half": "11012", "Q3": "11014", "Annual": "11011"}

corp_list_cache = []

def get_corp_code_list():
    """DART에서 전체 기업 고유번호 XML 파일을 다운로드하고 파싱하여 리스트로 반환합니다."""
    global corp_list_cache
    if corp_list_cache:
        return corp_list_cache

    if not os.path.exists(CORP_CODE_FILENAME):
        print(f"'{CORP_CODE_FILENAME}' 파일이 없어 DART에서 새로 다운로드합니다.")
        url = f"{BASE_URL}/corpCode.xml"
        params = {'crtfc_key': API_KEY}
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            with open('corp_code.zip', 'wb') as f:
                f.write(response.content)
            with zipfile.ZipFile('corp_code.zip', 'r') as zf:
                zf.extractall('.')
            os.remove('corp_code.zip')
        except requests.exceptions.RequestException as e:
            print(f"고유번호 파일 다운로드 실패: {e}")
            return []

    try:
        tree = ET.parse(CORP_CODE_FILENAME)
        root = tree.getroot()
        corp_list = [
            {'corp_name': corp.find('corp_name').text.strip(), 'corp_code': corp.find('corp_code').text.strip()}
            for corp in root.findall('list')
        ]
        corp_list_cache = corp_list
        return corp_list
    except (ET.ParseError, FileNotFoundError) as e:
        print(f"고유번호 파일 처리 오류: {e}")
        return []

def fetch_financial_data(corp_code, year, reprt_code, fs_div):
    """특정 연도, 특정 보고서, 특정 재무제표 종류의 재무 데이터를 DART API를 통해 가져옵니다."""
    url = f"{BASE_URL}/fnlttMultiAcnt.json"
    params = {
        'crtfc_key': API_KEY,
        'corp_code': corp_code,
        'bsns_year': year,
        'reprt_code': reprt_code,
        'fs_div': fs_div
    }
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get('status') != '000':
            return {}
        
        financial_info = {}
        for item in data.get('list', []):
            # 사용자 피드백 반영: API가 fs_div 파라미터와 관계없이 여러 종류를 반환할 경우를 대비하여,
            # 응답 목록 내에서 요청한 fs_div와 일치하는 항목만 처리하도록 명시적으로 필터링합니다.
            if item.get('fs_div') == fs_div:
                account_nm = item.get('account_nm')
                amount_str = item.get('thstrm_amount', '0').replace(',', '')
                amount = int(amount_str) if amount_str and amount_str != '-' else 0
                
                if account_nm in ['매출액', '영업수익']:
                    financial_info['revenue'] = amount
                elif account_nm == '영업이익':
                    financial_info['operating_profit'] = amount
                elif account_nm == '당기순이익':
                    financial_info['net_income'] = amount
        return financial_info
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError) as e:
        print(f"재무 데이터 조회 오류: {e}")
        return {}

def fetch_total_shares(corp_code, year, reprt_code):
    """총 주식 수를 DART API를 통해 가져옵니다."""
    url = f"{BASE_URL}/stockTotqySttus.json"
    params = {'crtfc_key': API_KEY, 'corp_code': corp_code, 'bsns_year': year, 'reprt_code': reprt_code}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get('status') != '000' or not data.get('list'):
            return None
        
        # 사용자 피드백 반영: se_nm 필드에 의존하지 않고, 응답 list에서 'istc_totqy'를 가진 첫 항목을 사용합니다.
        for item in data.get('list', []):
            if 'istc_totqy' in item:
                shares_str = item.get('istc_totqy', '0').replace(',', '')
                return int(shares_str) if shares_str and shares_str != '-' else 0
        
        return None # 'istc_totqy'를 가진 항목이 없는 경우

    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError) as e:
        print(f"총 주식 수 조회 오류: {e}")
        return None

import pandas as pd

def get_company_info(corp_code):
    """DART API를 통해 특정 기업의 상세 정보(상장 시장 포함)를 가져옵니다."""
    url = f"{BASE_URL}/company.json"
    params = {'crtfc_key': API_KEY, 'corp_code': corp_code}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == '000':
            return data
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        print(f"'{corp_code}' 기업 정보 조회 오류: {e}")
    return None

@app.route('/api/update-market-data') # SSE는 GET 요청을 사용합니다.
def update_market_data():
    def generate():
        try:
            yield "data: [시작] 상장 기업 정보 업데이트를 시작합니다...\n\n"
            
            # 1. KRX에서 엑셀 파일 다운로드
            yield "data: [1/4] KRX에서 상장사 목록 다운로드 중...\n\n"
            krx_url = 'https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13'
            krx_df = pd.read_html(krx_url, header=0)[0]
            
            # 2. DART 고유번호 목록 로드
            yield "data: [2/4] DART 고유번호 목록 로드 중...\n\n"
            dart_corps = get_corp_code_list()
            dart_df = pd.DataFrame(dart_corps)
            
            # 3. 데이터 병합
            yield "data: [3/4] 데이터 병합 및 필터링 중...\n\n"
            krx_df = krx_df[['회사명', '업종']]
            merged_df = pd.merge(dart_df, krx_df, left_on='corp_name', right_on='회사명', how='inner')
            
            # 4. 코스피/코스닥 정보 조회
            yield "data: [4/4] 코스피/코스닥 정보 조회 중 (시간이 소요됩니다)...\n\n"
            listed_companies = []
            total_corps = len(merged_df)
            for i, row in merged_df.iterrows():
                corp_code = row['corp_code']
                corp_name = row['corp_name']
                progress = (i + 1) / total_corps * 100
                
                # 진행 상황을 클라이언트로 스트리밍
                status_message = f"[{progress:.1f}%] ({i+1}/{total_corps}) {corp_name} 정보 확인 중..."
                yield f"data: {status_message}\n\n"
                
                time.sleep(0.1) # API 요청 제한 준수
                
                company_info = get_company_info(corp_code)
                if company_info and company_info.get('corp_cls') in ['Y', 'K']:
                    listed_companies.append({
                        'corp_code': corp_code,
                        'corp_name': corp_name,
                        'corp_cls': company_info['corp_cls']
                    })

            # 5. 최종 결과를 DB와 JSON 파일로 저장
            con = sqlite3.connect("finance_data.db")
            cur = con.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS market_info (
                    corp_code TEXT PRIMARY KEY,
                    corp_name TEXT,
                    corp_cls TEXT
                )
            ''')
            cur.executemany("INSERT OR REPLACE INTO market_info VALUES (:corp_code, :corp_name, :corp_cls)", listed_companies)
            con.commit()
            con.close()

            with open('market_data.json', 'w', encoding='utf-8') as f:
                json.dump(listed_companies, f, ensure_ascii=False, indent=4)
            
            final_message = f"[완료] 업데이트 완료: 총 {len(listed_companies)}개의 기업 정보를 DB와 파일에 저장했습니다."
            yield f"data: {final_message}\n\n"

        except Exception as e:
            error_message = f"[오류] 업데이트 중 오류 발생: {e}"
            yield f"data: {error_message}\n\n"

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/api/build-database')
def build_database():
    start_year = request.args.get('start_year', default=datetime.now().year, type=int)
    
    def generate():
        # 1. DB 연결 및 테이블 생성
        try:
            con = sqlite3.connect("finance_data.db")
            cur = con.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS financials (
                    corp_code TEXT,
                    year INTEGER,
                    quarter INTEGER,
                    revenue INTEGER,
                    operating_profit INTEGER,
                    net_income INTEGER,
                    total_shares INTEGER,
                    eps INTEGER,
                    PRIMARY KEY (corp_code, year, quarter)
                )
            ''')
            con.commit()
            yield "data: [1/3] 데이터베이스 연결 및 테이블 생성 완료\n\n"
        except Exception as e:
            yield f"data: [오류] 데이터베이스 초기화 실패: {e}\n\n"
            return

        # 2. 상장 기업 목록 로드
        try:
            with open('market_data.json', 'r', encoding='utf-8') as f:
                listed_companies = json.load(f)
            yield "data: [2/3] 상장 기업 목록 로드 완료\n\n"
        except Exception as e:
            yield f"data: [오류] market_data.json 로드 실패: {e}\n\n"
            con.close()
            return
            
        # 3. 재무 데이터 조회 및 DB 저장
        companies_to_process = listed_companies
        total_companies_to_process = len(companies_to_process)
        current_year = datetime.now().year
        metrics = ['revenue', 'operating_profit', 'net_income']
        
        for i, company in enumerate(companies_to_process):
            corp_code = company['corp_code']
            corp_name = company['corp_name']
            progress = (i + 1) / total_companies_to_process * 100
            
            status_message = f"[{progress:.1f}%] ({i+1}/{total_companies_to_process}) {corp_name} 데이터 수집 중..."
            yield f"data: {status_message}\n\n"

            for year in range(start_year, current_year + 1):
                year_str = str(year)
                
                # --- 연간 데이터가 모두 있는지 먼저 확인하여 건너뛰기 최적화 ---
                cur.execute("SELECT COUNT(*) FROM financials WHERE corp_code = ? AND year = ?", (corp_code, year))
                if cur.fetchone()[0] >= 4:
                    continue

                # --- API Calls for financial data (CFS first, then OFS) ---
                fs_div = 'CFS'
                q1_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Q1"], fs_div)
                q2_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Half"], fs_div)
                q3_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Q3"], fs_div)
                annual_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Annual"], fs_div)
                
                if not any([q1_data, q2_data, q3_data, annual_data]):
                    fs_div = 'OFS'
                    q1_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Q1"], fs_div)
                    q2_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Half"], fs_div)
                    q3_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Q3"], fs_div)
                    annual_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Annual"], fs_div)

                # --- API Calls for shares data ---
                shares_data = {
                    1: fetch_total_shares(corp_code, year_str, REPORT_CODES["Q1"]),
                    2: fetch_total_shares(corp_code, year_str, REPORT_CODES["Half"]),
                    3: fetch_total_shares(corp_code, year_str, REPORT_CODES["Q3"]),
                    4: fetch_total_shares(corp_code, year_str, REPORT_CODES["Annual"])
                }
                
                # --- Process and Insert Q1, Q2, Q3 ---
                quarterly_data = {1: q1_data, 2: q2_data, 3: q3_data}
                for q_num in range(1, 4):
                    data = quarterly_data.get(q_num, {})
                    if not data: continue
                    
                    shares = shares_data.get(q_num)
                    net_income = data.get('net_income')
                    eps = int(net_income / shares) if net_income is not None and shares is not None and shares > 0 else None
                    
                    cur.execute("INSERT OR IGNORE INTO financials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (corp_code, year, q_num, data.get('revenue'), data.get('operating_profit'), net_income, shares, eps))

                # --- Process and Insert Q4 ---
                if annual_data:
                    q4_metrics = {m: annual_data.get(m, 0) - sum(quarterly_data.get(q, {}).get(m, 0) for q in range(1, 4)) for m in metrics}
                    q4_shares = shares_data.get(4)
                    q4_net_income = q4_metrics.get('net_income')
                    q4_eps = int(q4_net_income / q4_shares) if q4_net_income is not None and q4_shares is not None and q4_shares > 0 else None
                    
                    cur.execute("INSERT OR IGNORE INTO financials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (corp_code, year, 4, q4_metrics.get('revenue'), q4_metrics.get('operating_profit'), q4_net_income, q4_shares, q4_eps))
            
            con.commit()

        con.close()
        yield "data: [완료] 데이터베이스 구축이 완료되었습니다.\n\n"

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/api/db-status')
def db_status():
    company_name = request.args.get('company_name')
    if not company_name:
        return jsonify({"error": "회사 이름이 필요합니다."}), 400

    corp_code = None
    # corp_code를 찾기 위해 market_data.json을 사용 (더 효율적인 방법은 DB에 corp_name도 저장하는 것)
    try:
        with open('market_data.json', 'r', encoding='utf-8') as f:
            market_data = json.load(f)
            for company in market_data:
                if company['corp_name'] == company_name:
                    corp_code = company['corp_code']
                    break
    except FileNotFoundError:
        return jsonify({"error": "'market_data.json' 파일이 없습니다. 먼저 상장 기업 정보 업데이트를 실행해주세요."}), 400
    
    if not corp_code:
        return jsonify({"error": f"'{company_name}'을(를) 찾을 수 없습니다."}), 404

    try:
        con = sqlite3.connect("finance_data.db")
        cur = con.cursor()
        cur.execute("SELECT year, quarter FROM financials WHERE corp_code = ? ORDER BY year, quarter", (corp_code,))
        rows = cur.fetchall()
        con.close()

        status = {}
        for row in rows:
            year, quarter = row
            if year not in status:
                status[year] = []
            status[year].append(quarter)
            
        return jsonify(status)

    except sqlite3.OperationalError:
        return jsonify({"error": "'finance_data.db' 파일이 없거나 테이블이 없습니다. 먼저 DB 구축을 실행해주세요."}), 400
    except Exception as e:
        return jsonify({"error": f"DB 조회 중 오류 발생: {e}"}), 500

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/api/financials')
def get_financials():
    company_name = request.args.get('company_name')
    start_year_str = request.args.get('start_year')
    end_year_str = request.args.get('end_year')
    fs_div = request.args.get('fs_div', 'CFS') # 기본값은 'CFS' (연결)
    
    print(f"\n요청 수신: {company_name}, {start_year_str}년-{end_year_str}년, 재무제표: {fs_div}")

    if not all([company_name, start_year_str, end_year_str]):
        return jsonify({"error": "회사 이름, 시작 연도, 종료 연도를 모두 입력해주세요."}), 400

    try:
        start_year = int(start_year_str)
        end_year = int(end_year_str)
    except ValueError:
        return jsonify({"error": "연도는 숫자로 입력해주세요."}), 400

    try:
        with open('market_data.json', 'r', encoding='utf-8') as f:
            corp_list = json.load(f)
    except FileNotFoundError:
        return jsonify({"error": "'market_data.json' 파일을 찾을 수 없습니다. 먼저 상장 기업 정보 업데이트를 실행해주세요."}), 400
    
    if not corp_list:
        return jsonify({"error": "상장 기업 목록이 비어있습니다."}), 500

    selected_corp = next((c for c in corp_list if c['corp_name'] == company_name), None)
    if not selected_corp:
        print(f"오류: '{company_name}'을(를) 찾을 수 없습니다.")
        return jsonify({"error": f"'{company_name}'을(를) 찾을 수 없습니다."}), 404

    corp_code = selected_corp['corp_code']
    print(f"'{company_name}'의 기업 코드({corp_code})를 찾았습니다.")
    
    # 성장률 계산을 위해 요청된 시작 연도보다 1년 더 일찍 데이터 조회 시작
    all_years_data = {}
    metrics = ['revenue', 'operating_profit', 'net_income']
    
    for year in range(start_year - 1, end_year + 1):
        year_str = str(year)
        print(f"--- {year_str}년 데이터 조회 시작 ---")

        # 1. 재무 데이터 가져오기
        q1_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Q1"], fs_div)
        q2_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Half"], fs_div)
        q3_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Q3"], fs_div)
        annual_data = fetch_financial_data(corp_code, year_str, REPORT_CODES["Annual"], fs_div)

        # 2. 4분기 실적 계산
        q4_data = {}
        if not annual_data:
            for m in metrics: q4_data[m] = None
        else:
            for m in metrics:
                q4_data[m] = annual_data.get(m, 0) - (q1_data.get(m, 0) + q2_data.get(m, 0) + q3_data.get(m, 0))

        # 3. 총 주식 수 가져오기
        shares_data = {
            "Q1": fetch_total_shares(corp_code, year_str, REPORT_CODES["Q1"]),
            "Q2": fetch_total_shares(corp_code, year_str, REPORT_CODES["Half"]),
            "Q3": fetch_total_shares(corp_code, year_str, REPORT_CODES["Q3"]),
            "Q4": fetch_total_shares(corp_code, year_str, REPORT_CODES["Annual"]),
        }
        annual_shares = shares_data["Q4"]
        for q in ["Q1", "Q2", "Q3"]:
            if shares_data[q] is None: shares_data[q] = annual_shares

        # 4. 해당 연도 데이터 정리
        all_years_data[year_str] = {
            "Q1": q1_data, "Q2": q2_data, "Q3": q3_data, "Q4": q4_data, "Annual": annual_data
        }
        for q_key, s_val in shares_data.items():
            if q_key in all_years_data[year_str]:
                all_years_data[year_str][q_key]["total_shares"] = s_val
        if "total_shares" not in all_years_data[year_str]["Annual"]:
            all_years_data[year_str]["Annual"]["total_shares"] = shares_data["Q4"]
        
        print(f"--- {year_str}년 데이터 조회 완료 ---")

    # 5. EPS 계산
    print("--- 모든 기간 EPS 계산 시작 ---")
    for year_str, year_data in all_years_data.items():
        for quarter, quarter_data in year_data.items():
            if not quarter_data: continue
            net_income = quarter_data.get('net_income')
            total_shares = quarter_data.get('total_shares')
            eps = None
            if net_income is not None and total_shares is not None and total_shares > 0:
                eps = int(net_income / total_shares)
            quarter_data['eps'] = eps
    print("--- 모든 기간 EPS 계산 완료 ---")

    # 6. TTM (Trailing Twelve Months) 데이터 계산 for Charts
    print("--- TTM 데이터 계산 시작 ---")
    quarterly_data_flat = []
    for year in sorted(all_years_data.keys(), key=int):
        for q_num in range(1, 5):
            q_key = f"Q{q_num}"
            if q_key in all_years_data[year] and all_years_data[year][q_key]:
                entry = all_years_data[year][q_key]
                entry['label'] = f"{year}.{q_num}Q"
                quarterly_data_flat.append(entry)

    chart_data = {'revenue': [], 'operating_profit': [], 'net_income': [], 'eps': []}
    metrics_for_chart = list(chart_data.keys())
    if len(quarterly_data_flat) >= 4:
        for i in range(3, len(quarterly_data_flat)):
            for metric in metrics_for_chart:
                relevant_quarters = quarterly_data_flat[i-3 : i+1]
                if all(q.get(metric) is not None for q in relevant_quarters):
                    ttm_value = sum(q.get(metric, 0) for q in relevant_quarters)
                    chart_data[metric].append({'x': quarterly_data_flat[i]['label'], 'y': ttm_value})
    print("--- TTM 데이터 계산 완료 ---")

    # 7. 성장률 계산 및 최종 테이블 결과 생성
    table_data = {}
    for year in range(start_year, end_year + 1):
        year_str = str(year)
        prev_year_str = str(year - 1)
        if year_str not in all_years_data: continue
        
        table_data[year_str] = all_years_data[year_str]
        prev_year_data = all_years_data.get(prev_year_str)

        for quarter in ["Q1", "Q2", "Q3", "Q4", "Annual"]:
            if quarter not in table_data[year_str] or not table_data[year_str][quarter]: continue
            for metric in ['revenue', 'operating_profit', 'net_income', 'eps']:
                growth_key = f"{metric}_growth"
                current_val = table_data[year_str][quarter].get(metric)
                prev_val = prev_year_data.get(quarter, {}).get(metric) if prev_year_data else None
                
                growth_rate = None
                if current_val is not None and prev_val is not None and prev_val != 0:
                    growth_rate = ((current_val - prev_val) / abs(prev_val)) * 100
                
                table_data[year_str][quarter][growth_key] = growth_rate

    print("모든 데이터 조회 및 계산 완료. 결과를 전송합니다.")
    return jsonify({"table_data": table_data, "chart_data": chart_data})

@app.route('/api/screener', methods=['POST'])
def run_screener():
    conditions = request.json
    print(f"\n스크리너 요청 수신: {conditions}")

    try:
        con = sqlite3.connect("finance_data.db")
        # 모든 기업의 성장률을 계산하는 것은 매우 무거운 작업이므로,
        # 이 예제에서는 최근 2년치 데이터가 있는 기업들을 대상으로 필터링합니다.
        # 실제 서비스에서는 이 쿼리를 더 최적화해야 합니다.
        
        # 동적 쿼리 생성을 위한 준비
        base_query = """
            WITH GrowthData AS (
                SELECT
                    corp_code,
                    year,
                    quarter,
                    revenue,
                    operating_profit,
                    net_income,
                    LAG(revenue, 4) OVER (PARTITION BY corp_code ORDER BY year, quarter) as prev_y_revenue,
                    LAG(operating_profit, 4) OVER (PARTITION BY corp_code ORDER BY year, quarter) as prev_y_op,
                    LAG(net_income, 4) OVER (PARTITION BY corp_code ORDER BY year, quarter) as prev_y_ni
                FROM financials
            ),
            AggregatedGrowth AS (
                SELECT
                    g.corp_code,
                    m.corp_name,
                    m.corp_cls,
                    -- 분기 성장률 계산 (최근 N분기)
                    AVG(CASE WHEN g.prev_y_revenue > 0 THEN (g.revenue - g.prev_y_revenue) * 100.0 / g.prev_y_revenue ELSE NULL END) as avg_q_revenue_growth,
                    AVG(CASE WHEN g.prev_y_op <> 0 THEN (g.operating_profit - g.prev_y_op) * 100.0 / ABS(g.prev_y_op) ELSE NULL END) as avg_q_op_profit_growth,
                    AVG(CASE WHEN g.prev_y_ni <> 0 THEN (g.net_income - g.prev_y_ni) * 100.0 / ABS(g.prev_y_ni) ELSE NULL END) as avg_q_net_income_growth
                FROM GrowthData g
                JOIN market_info m ON g.corp_code = m.corp_code
                WHERE g.year >= (SELECT MAX(year) FROM financials) - 2 -- 최근 3년치 데이터로 계산
                GROUP BY g.corp_code
            )
            SELECT * FROM AggregatedGrowth WHERE 1=1
        """
        
        params = []
        # 사용자가 선택한 조건에 따라 WHERE 절 동적 추가 (HAVING 절 대신 WHERE 사용)
        if conditions.get('qRevenueCheck'):
            base_query += " AND avg_q_revenue_growth >= ?"
            params.append(float(conditions['qRevenue']['growth']))
        if conditions.get('qOpProfitCheck'):
            base_query += " AND avg_q_op_profit_growth >= ?"
            params.append(float(conditions['qOpProfit']['growth']))
        if conditions.get('qNetIncomeCheck'):
            base_query += " AND avg_q_net_income_growth >= ?"
            params.append(float(conditions['qNetIncome']['growth']))
        
        # (연간 성장률 조건은 쿼리가 더 복잡해지므로 이 예제에서는 생략)

        base_query += " LIMIT 100" # 너무 많은 결과를 방지하기 위해 제한

        df = pd.read_sql_query(base_query, con, params=params)
        con.close()

        results = []
        for _, row in df.iterrows():
            results.append({
                "corp_name": row['corp_name'],
                "market": "코스피" if row['corp_cls'] == 'Y' else "코스닥",
                "avg_q_revenue_growth": row['avg_q_revenue_growth'] or 0,
                "avg_q_op_profit_growth": row['avg_q_op_profit_growth'] or 0,
                "avg_q_net_income_growth": row['avg_q_net_income_growth'] or 0,
                "avg_y_revenue_growth": 0, # Dummy
                "avg_y_op_profit_growth": 0, # Dummy
                "avg_y_net_income_growth": 0, # Dummy
            })
            
        return jsonify(results)

    except Exception as e:
        return jsonify({"error": f"스크리닝 중 오류 발생: {e}"}), 500


if __name__ == '__main__':
    get_corp_code_list() # 서버 시작 시 기업 코드 미리 로드
    app.run(host='0.0.0.0', port=5003, debug=True)
