from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import requests
import zipfile
import xml.etree.ElementTree as ET
import os
import json
import time
from dotenv import load_dotenv

load_dotenv() # .env 파일에서 환경 변수를 로드합니다.

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
    url = f"{BASE_URL}/fnlttSinglAcnt.json"
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
                amount = int(amount_str) if amount_str else 0
                
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
                return int(shares_str) if shares_str else 0
        
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

            # 5. 최종 결과를 JSON 파일로 저장
            with open('market_data.json', 'w', encoding='utf-8') as f:
                json.dump(listed_companies, f, ensure_ascii=False, indent=4)
            
            final_message = f"[완료] 업데이트 완료: 총 {len(listed_companies)}개의 기업 정보를 저장했습니다."
            yield f"data: {final_message}\n\n"

        except Exception as e:
            error_message = f"[오류] 업데이트 중 오류 발생: {e}"
            yield f"data: {error_message}\n\n"

    return Response(generate(), mimetype='text/event-stream')

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

    corp_list = get_corp_code_list()
    if not corp_list:
        return jsonify({"error": "기업 목록을 불러올 수 없습니다."}), 500

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
        with open('market_data.json', 'r', encoding='utf-8') as f:
            listed_companies = json.load(f)
    except FileNotFoundError:
        return jsonify({"error": "'market_data.json' 파일을 찾을 수 없습니다. 먼저 상장 기업 정보 업데이트를 실행해주세요."}), 400
    except json.JSONDecodeError:
        return jsonify({"error": "'market_data.json' 파일이 손상되었습니다. 업데이트를 다시 실행해주세요."}), 500

    if not listed_companies:
        return jsonify({"error": "상장 기업 목록이 비어있습니다. 업데이트를 실행해주세요."}), 500

    results = []
    total_companies = len(listed_companies)
    
    from datetime import datetime
    import time

    for i, company in enumerate(listed_companies):
        corp_code = company['corp_code']
        corp_name = company['corp_name']
        print(f"({i+1}/{total_companies}) '{corp_name}' 스크리닝 시작...")

        # DART API는 초당 요청 제한이 있을 수 있으므로 약간의 지연을 줍니다.
        time.sleep(0.1) 

        try:
            # 여기에 실제 성장률 계산 및 조건 필터링 로직이 들어갑니다.
            # 이 로직은 매우 길고 복잡하므로, 지금은 개념 증명을 위해
            # 모든 회사가 조건을 통과하고, 더미 성장률을 반환한다고 가정합니다.
            # 실제 구현을 위해서는 이 부분을 상세한 재무 데이터 조회 및 계산 코드로 채워야 합니다.
            
            passes_conditions = True # 임시로 통과 처리
            
            if passes_conditions:
                results.append({
                    "corp_name": corp_name,
                    "market": "코스피" if company['corp_cls'] == 'Y' else "코스닥",
                    "avg_q_revenue_growth": 15.2, # Dummy data
                    "avg_q_op_profit_growth": 20.1, # Dummy data
                    "avg_q_net_income_growth": 25.5, # Dummy data
                    "avg_y_revenue_growth": 10.0, # Dummy data
                    "avg_y_op_profit_growth": 12.3, # Dummy data
                    "avg_y_net_income_growth": 14.8, # Dummy data
                })

                # 테스트를 위해 결과를 15개로 제한
                if len(results) >= 15:
                    print("결과가 15개에 도달하여 스크리닝을 중단합니다.")
                    break
        except Exception as e:
            print(f"'{corp_name}' 스크리닝 중 오류 발생: {e}")
            continue

    return jsonify(results)


if __name__ == '__main__':
    get_corp_code_list() # 서버 시작 시 기업 코드 미리 로드
    app.run(debug=True, port=5003)
