"""鏌ユ壘绗﹀悎鏉′欢鐨勮偂绁細甯傜泩鐜?00浠ュ唴锛屽勾鎶ヤ笟缁╁巻鍙叉渶濂斤紝鑲′环鏈垱鏂伴珮"""

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
    return stock_list


def get_daily_kline(pro: ts.pro_api, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """鑾峰彇鏃绾挎暟鎹?""
    try:
        df = pro.daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,open,high,low,close,vol,amount'
        )
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"鑾峰彇{ts_code}鏃绾垮け璐ワ細{e}")
        return pd.DataFrame()


def get_financial_data(pro: ts.pro_api, ts_code: str) -> dict:
    """鑾峰彇璐㈠姟鏁版嵁锛歅E鍜屽噣鍒╂鼎"""
    try:
        today = datetime.now()
        end_date = today.strftime('%Y%m%d')
        start_date = (today - timedelta(days=30)).strftime('%Y%m%d')
        
        basic_data = pro.daily_basic(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,pe_ttm'
        )
        
        pe_ttm = None
        if basic_data is not None and not basic_data.empty:
            basic_data = basic_data.sort_values('trade_date', ascending=False)
            pe_ttm = basic_data.iloc[0]['pe_ttm']
        
        income_data = pro.income(
            ts_code=ts_code,
            start_date='20100101',
            end_date=end_date,
            fields='ts_code,end_date,n_income,ann_date'
        )
        
        net_profit_history = []
        if income_data is not None and not income_data.empty:
            income_data = income_data.sort_values('end_date', ascending=False)
            net_profit_history = income_data['n_income'].dropna().tolist()
        
        return {
            'pe_ttm': pe_ttm,
            'net_profit_history': net_profit_history
        }
    except Exception as e:
        print(f"鑾峰彇{ts_code}璐㈠姟鏁版嵁澶辫触锛歿e}")
        return {
            'pe_ttm': None,
            'net_profit_history': []
        }


def check_conditions(pro: ts.pro_api, ts_code: str, name: str) -> dict:
    """妫€鏌ヨ偂绁ㄦ槸鍚︾鍚堝悇涓潯浠?""
    today = datetime.now()
    five_years_ago = today - timedelta(days=5*365)
    end_date = today.strftime('%Y%m%d')
    start_date = five_years_ago.strftime('%Y%m%d')
    
    daily_data = get_daily_kline(pro, ts_code, start_date, end_date)
    
    if daily_data.empty or len(daily_data) < 252:
        return None
    
    financial_data = get_financial_data(pro, ts_code)
    pe_ttm = financial_data['pe_ttm']
    net_profit_history = financial_data['net_profit_history']
    
    # 鏉′欢1: 甯傜泩鐜?00浠ュ唴
    cond1 = pe_ttm is not None and pe_ttm > 0 and pe_ttm <= 100
    
    # 鏉′欢2: 骞存姤涓氱哗鍘嗗彶鏈€濂?    cond2 = False
    if len(net_profit_history) >= 2:
        latest_profit = net_profit_history[0]
        if not pd.isna(latest_profit) and latest_profit > 0:
            cond2 = True
            for profit in net_profit_history[1:]:
                if not pd.isna(profit) and profit > latest_profit:
                    cond2 = False
                    break
    
    # 鏉′欢3: 鑲′环鏈垱鏂伴珮锛堣窛绂诲巻鍙查珮鐐规湁5%浠ヤ笂绌洪棿锛?    all_time_high = daily_data['high'].max()
    latest_close = daily_data.iloc[-1]['close']
    cond3 = latest_close < all_time_high * 0.95
    
    return {
        'ts_code': ts_code,
        'name': name,
        'pe_ttm': pe_ttm,
        'cond1': cond1,
        'cond2': cond2,
        'cond3': cond3,
        'all_time_high': all_time_high,
        'latest_close': latest_close,
        'distance_to_high': (all_time_high - latest_close) / all_time_high * 100 if all_time_high > 0 else 0
    }


def main():
    """涓诲嚱鏁?""
    try:
        pro = init_client()
        print("瀹㈡埛绔垵濮嬪寲鎴愬姛")
        
        stock_list = get_stock_list(pro)
        print(f"鍏辫幏鍙?{len(stock_list)} 鍙偂绁?)
        
        all_results = []
        total = len(stock_list)
        analyzed_count = 0
        
        for idx, stock in stock_list.iterrows():
            ts_code = stock['ts_code']
            name = stock['name']
            
            if ts_code.endswith('.BJ'):
                analyzed_count += 1
                continue
            
            print(f"妫€鏌ヤ腑 [{idx+1}/{total}]: {ts_code} {name}")
            
            try:
                result = check_conditions(pro, ts_code, name)
                if result:
                    all_results.append(result)
            except Exception as e:
                print(f"  妫€鏌?{ts_code} 鏃跺嚭閿? {e}")
                continue
            
            analyzed_count += 1
            
            if analyzed_count % 50 == 0:
                import time
                time.sleep(0.5)
        
        if all_results:
            df = pd.DataFrame(all_results)
            
            # 鍒嗗埆绛涢€夌鍚堝悇涓潯浠剁殑鑲＄エ
            cond1_df = df[df['cond1']].copy()
            cond2_df = df[df['cond2']].copy()
            cond3_df = df[df['cond3']].copy()
            
            # 绛涢€夌鍚堜换鎰忎袱涓潯浠剁殑鑲＄エ
            cond1_and_2_df = df[(df['cond1']) & (df['cond2'])].copy()
            cond1_and_3_df = df[(df['cond1']) & (df['cond3'])].copy()
            cond2_and_3_df = df[(df['cond2']) & (df['cond3'])].copy()
            
            # 绛涢€夌鍚堟墍鏈変笁涓潯浠剁殑鑲＄エ
            all_conds_df = df[(df['cond1']) & (df['cond2']) & (df['cond3'])].copy()
            
            output_file = "sng缁撴灉.xlsx"
            
            # 淇濆瓨鍒癊xcel鐨勪笉鍚屽伐浣滆〃
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                cond1_df.to_excel(writer, sheet_name='鏉′欢1-PE<100', index=False)
                cond2_df.to_excel(writer, sheet_name='鏉′欢2-鍘嗗彶鏈€濂戒笟缁?, index=False)
                cond3_df.to_excel(writer, sheet_name='鏉′欢3-鏈垱鏂伴珮', index=False)
                cond1_and_2_df.to_excel(writer, sheet_name='鏉′欢1+2', index=False)
                cond1_and_3_df.to_excel(writer, sheet_name='鏉′欢1+3', index=False)
                cond2_and_3_df.to_excel(writer, sheet_name='鏉′欢2+3', index=False)
                all_conds_df.to_excel(writer, sheet_name='鍏ㄩ儴3涓潯浠?, index=False)
            
            print(f"\n缁熻缁撴灉锛?)
            print(f"  鏉′欢1锛堝競鐩堢巼100浠ュ唴锛? {len(cond1_df)} 鍙?)
            print(f"  鏉′欢2锛堝巻鍙叉渶濂戒笟缁╋級: {len(cond2_df)} 鍙?)
            print(f"  鏉′欢3锛堟湭鍒涙柊楂橈級: {len(cond3_df)} 鍙?)
            print(f"  鏉′欢1+2: {len(cond1_and_2_df)} 鍙?)
            print(f"  鏉′欢1+3: {len(cond1_and_3_df)} 鍙?)
            print(f"  鏉′欢2+3: {len(cond2_and_3_df)} 鍙?)
            print(f"  鍏ㄩ儴3涓潯浠? {len(all_conds_df)} 鍙?)
            print(f"\n缁撴灉宸蹭繚瀛樺埌: {output_file}")
        else:
            print("\n鏈幏鍙栧埌鏈夋晥鑲＄エ鏁版嵁")
            
    except Exception as e:
        print(f"绋嬪簭鎵ц鍑洪敊: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()


