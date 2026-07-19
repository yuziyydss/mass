﻿﻿﻿"""鎵惧嚭绗﹀悎鏈圞绾垮簳閮ㄦ潯浠剁殑鑲＄エ"""

import os
import pandas as pd
import numpy as np
import tushare as ts
from datetime import datetime, timedelta
from dotenv import load_dotenv


def init_client() -> ts.pro_api:
    """鍒濆鍖杢ushare瀹㈡埛绔?""
    load_dotenv()
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN")
    if not token:`r`n        raise RuntimeError("未找到 TUSHARE_TOKEN，请检查 .env")
    ts.set_token(token)
    return ts.pro_api()


def get_stock_list(pro: ts.pro_api) -> pd.DataFrame:
    """鑾峰彇鎵€鏈堿鑲″垪琛紝杩囨护鎺夊寳浜ゆ墍鑲＄エ"""
    stock_list = pro.stock_basic(
        exchange='',
        list_status='L',
        fields='ts_code,symbol,name,area,industry,list_date'
    )
    
    stock_list = stock_list[~stock_list["symbol"].str.startswith("8")]
    stock_list = stock_list[~stock_list["symbol"].str.startswith("9")]
    
    print(f"杩囨护鍚庡墿浣欒偂绁ㄦ暟閲? {len(stock_list)}")
    return stock_list


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """璁＄畻RSI鎸囨爣"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def get_monthly_kline(pro: ts.pro_api, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """鑾峰彇鏈圞绾挎暟鎹?""
    try:
        df = pro.monthly(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,open,high,low,close,vol,amount'
        )
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"鑾峰彇{ts_code}鏈圞绾垮け璐ワ細{e}")
        return pd.DataFrame()


def get_financial_data(pro: ts.pro_api, ts_code: str) -> dict:
    """鑾峰彇璐㈠姟鏁版嵁锛歅E鍜屽噣鍒╂鼎"""
    try:
        today = datetime.now().strftime('%Y%m%d')
        basic_data = pro.daily_basic(
            ts_code=ts_code,
            trade_date=today,
            fields='ts_code,pe_ttm'
        )
        
        pe_ttm = None
        if basic_data is not None and not basic_data.empty:
            pe_ttm = basic_data.iloc[0]['pe_ttm']
        
        income_data = pro.income(
            ts_code=ts_code,
            start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
            end_date=today,
            fields='ts_code,end_date,n_income'
        )
        
        net_profit = None
        if income_data is not None and not income_data.empty:
            income_data = income_data.sort_values('end_date', ascending=False)
            net_profit = income_data.iloc[0]['n_income']
        
        return {
            'pe_ttm': pe_ttm,
            'net_profit': net_profit
        }
    except Exception as e:
        print(f"鑾峰彇{ts_code}璐㈠姟鏁版嵁澶辫触锛歿e}")
        return {
            'pe_ttm': None,
            'net_profit': None
        }


def check_conditions(pro: ts.pro_api, ts_code: str, name: str) -> dict:
    """妫€鏌ヨ偂绁ㄦ槸鍚︾鍚堟墍鏈夋潯浠?""
    today = datetime.now()
    three_years_ago = today - timedelta(days=3*365)
    end_date = today.strftime('%Y%m%d')
    start_date = three_years_ago.strftime('%Y%m%d')
    
    monthly_data = get_monthly_kline(pro, ts_code, start_date, end_date)
    
    if monthly_data.empty or len(monthly_data) < 36:
        return None
    
    first_close = monthly_data.iloc[0]['close']
    last_close = monthly_data.iloc[-1]['close']
    decline = (first_close - last_close) / first_close * 100
    
    if decline <= 40:
        return None
    
    monthly_data['rsi'] = calculate_rsi(monthly_data['close'])
    latest_rsi = monthly_data.iloc[-1]['rsi']
    
    if pd.isna(latest_rsi) or latest_rsi >= 35:
        return None
    
    if len(monthly_data) < 2:
        return None
    
    latest_vol = monthly_data.iloc[-1]['vol']
    prev_vol = monthly_data.iloc[-2]['vol']
    latest_close = monthly_data.iloc[-1]['close']
    prev_close = monthly_data.iloc[-2]['close']
    
    price_increase = latest_close > prev_close
    
    if not price_increase:
        return None
    
    financial_data = get_financial_data(pro, ts_code)
    pe_ttm = financial_data['pe_ttm']
    net_profit = financial_data['net_profit']
    
    if pe_ttm is None or pe_ttm >= 40:
        return None
    
    if net_profit is None or net_profit <= 0:
        return None
    
    if len(monthly_data) < 5:
        return None
    
    monthly_data['ma5'] = monthly_data['close'].rolling(window=5).mean()
    
    ma5_values = monthly_data['ma5'].dropna().values
    if len(ma5_values) < 3:
        return None
    
    recent_ma5 = ma5_values[-3:]
    is_flat_or_up = (recent_ma5[-1] >= recent_ma5[-2] * 0.99)
    
    if not is_flat_or_up:
        return None
    
    return {
        'ts_code': ts_code,
        'name': name,
        'decline_3y': decline,
        'latest_rsi': latest_rsi,
        'pe_ttm': pe_ttm,
        'net_profit': net_profit,
        'latest_close': last_close,
        'first_close': first_close
    }


def main():
    """涓诲嚱鏁?""
    try:
        pro = init_client()
        print("瀹㈡埛绔垵濮嬪寲鎴愬姛")
        
        stock_list = get_stock_list(pro)
        print(f"鍏辫幏鍙?{len(stock_list)} 鍙偂绁?)
        
        results = []
        total = len(stock_list)
        
        for idx, row in stock_list.iterrows():
            ts_code = row['ts_code']
            name = row['name']
            
            print(f"妫€鏌ヤ腑 [{idx+1}/{total}]: {ts_code} {name}")
            
            try:
                result = check_conditions(pro, ts_code, name)
                if result:
                    results.append(result)
                    print(f"  鉁?鎵惧埌绗﹀悎鏉′欢鐨勮偂绁? {ts_code} {name}")
            except Exception as e:
                print(f"  妫€鏌?{ts_code} 鏃跺嚭閿? {e}")
                continue
            
            if idx > 0 and idx % 50 == 0:
                import time
                time.sleep(0.1)
        
        if results:
            df = pd.DataFrame(results)
            output_file = "鏈坘搴曢儴缁撴灉.csv"
            df.to_csv(output_file, index=False, encoding='utf-8-sig')
            print(f"\n鍏辨壘鍒?{len(results)} 鍙鍚堟潯浠剁殑鑲＄エ")
            print(f"缁撴灉宸蹭繚瀛樺埌: {output_file}")
            
            print("\n绗﹀悎鏉′欢鐨勮偂绁細")
            print(df[['ts_code', 'name', 'decline_3y', 'latest_rsi', 'pe_ttm', 'net_profit']])
        else:
            print("\n鏈壘鍒扮鍚堟潯浠剁殑鑲＄エ")
            
    except Exception as e:
        print(f"绋嬪簭鎵ц鍑洪敊: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()


