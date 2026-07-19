"""鏌ユ壘绗﹀悎鑲′环搴曢儴4澶ц　閲忔爣鍑嗙殑鑲＄エ"""

import os
import pandas as pd
import numpy as np
import tushare as ts
from datetime import datetime, timedelta
from dotenv import load_dotenv


def init_client():
    load_dotenv()
    token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN")
    if not token:`r`n        raise RuntimeError("未找到 TUSHARE_TOKEN，请检查 .env")
    ts.set_token(token)
    return ts.pro_api()


def get_stock_list(pro):
    stock_list = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,list_date')
    return stock_list


def get_daily_kline(pro, ts_code, start_date, end_date):
    try:
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date, fields='ts_code,trade_date,open,high,low,close,vol,amount')
        if df is not None and not df.empty:
            df = df.sort_values('trade_date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"鑾峰彇{ts_code}鏃绾垮け璐ワ細{e}")
        return pd.DataFrame()


def calculate_rsi(data, period=14):
    delta = data['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(data, fast=12, slow=26, signal=9):
    ema_fast = data['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = data['close'].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return {'macd_line': macd_line, 'signal_line': signal_line, 'histogram': histogram}


def calculate_kdj(data, n=9, m1=3, m2=3):
    low_list = data['low'].rolling(n, min_periods=1).min()
    high_list = data['high'].rolling(n, min_periods=1).max()
    rsv = (data['close'] - low_list) / (high_list - low_list) * 100
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return {'k': k, 'd': d, 'j': j}


def get_financial_data(pro, ts_code):
    try:
        today = datetime.now()
        end_date = today.strftime('%Y%m%d')
        start_date = (today - timedelta(days=30)).strftime('%Y%m%d')
        basic_data = pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date, fields='ts_code,trade_date,pe_ttm,pb,ps,dv_ratio')
        pe_ttm = pb = dv_ratio = None
        if basic_data is not None and not basic_data.empty:
            basic_data = basic_data.sort_values('trade_date', ascending=False)
            pe_ttm = basic_data.iloc[0]['pe_ttm']
            pb = basic_data.iloc[0]['pb']
            dv_ratio = basic_data.iloc[0]['dv_ratio']
        return {'pe_ttm': pe_ttm, 'pb': pb, 'dv_ratio': dv_ratio}
    except Exception as e:
        print(f"鑾峰彇{ts_code}璐㈠姟鏁版嵁澶辫触锛歿e}")
        return {'pe_ttm': None, 'pb': None, 'dv_ratio': None}


def check_bottom_conditions(pro, ts_code, name):
    today = datetime.now()
    five_years_ago = today - timedelta(days=5*365)
    end_date = today.strftime('%Y%m%d')
    start_date = five_years_ago.strftime('%Y%m%d')
    daily_data = get_daily_kline(pro, ts_code, start_date, end_date)
    if daily_data.empty or len(daily_data) < 252:
        return None
    financial_data = get_financial_data(pro, ts_code)
    conditions = {}
    
    recent_60 = daily_data.tail(60)
    if len(recent_60) >= 60:
        vol_high = recent_60['vol'].max()
        recent_5 = daily_data.tail(5)
        all_low_volume = all((vol < vol_high * 0.2) and (vol > 0) for vol in recent_5['vol'])
        conditions['cond1_volume'] = all_low_volume
    else:
        conditions['cond1_volume'] = False
    
    recent_30 = daily_data.tail(30)
    if len(recent_30) >= 30:
        lows = []
        for i in range(3):
            segment = recent_30.iloc[i*10:(i+1)*10]
            if not segment.empty:
                lows.append(segment['low'].min())
        conditions['cond2_price'] = len(lows) >= 3 and lows[0] < lows[1] < lows[2]
    else:
        conditions['cond2_price'] = False
    
    pb = financial_data['pb']
    dv_ratio = financial_data['dv_ratio']
    cond3_valuation = False
    if pb is not None and pb < 1:
        cond3_valuation = True
    elif dv_ratio is not None and dv_ratio > 3:
        cond3_valuation = True
    conditions['cond3_valuation'] = cond3_valuation
    
    rsi = calculate_rsi(daily_data)
    macd = calculate_macd(daily_data)
    recent_30_data = daily_data.tail(30).copy()
    recent_30_data['rsi'] = rsi.tail(30)
    recent_30_data['macd'] = macd['macd_line'].tail(30)
    cond4_divergence = False
    if len(recent_30_data) >= 30:
        price_lows = recent_30_data.nsmallest(3, 'low')
        if len(price_lows) >= 2:
            rsi_divergence = price_lows.iloc[0]['low'] < price_lows.iloc[1]['low'] and recent_30_data.loc[price_lows.index[0], 'rsi'] > recent_30_data.loc[price_lows.index[1], 'rsi']
            macd_divergence = price_lows.iloc[0]['low'] < price_lows.iloc[1]['low'] and recent_30_data.loc[price_lows.index[0], 'macd'] > recent_30_data.loc[price_lows.index[1], 'macd']
            cond4_divergence = rsi_divergence or macd_divergence
    conditions['cond4_divergence'] = cond4_divergence
    
    conditions_met = sum([conditions['cond1_volume'], conditions['cond2_price'], conditions['cond3_valuation'], conditions['cond4_divergence']])
    return {
        'ts_code': ts_code,
        'name': name,
        'pe_ttm': financial_data['pe_ttm'],
        'pb': financial_data['pb'],
        'dv_ratio': financial_data['dv_ratio'],
        'cond1_volume': conditions['cond1_volume'],
        'cond2_price': conditions['cond2_price'],
        'cond3_valuation': conditions['cond3_valuation'],
        'cond4_divergence': conditions['cond4_divergence'],
        'conditions_met': conditions_met,
        'latest_close': daily_data.iloc[-1]['close'],
        'latest_vol': daily_data.iloc[-1]['vol']
    }


def main():
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
                result = check_bottom_conditions(pro, ts_code, name)
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
            cond1_df = df[df['cond1_volume']].copy()
            cond2_df = df[df['cond2_price']].copy()
            cond3_df = df[df['cond3_valuation']].copy()
            cond4_df = df[df['cond4_divergence']].copy()
            cond2_plus_df = df[df['conditions_met'] >= 2].copy()
            cond3_plus_df = df[df['conditions_met'] >= 3].copy()
            all_conds_df = df[df['conditions_met'] == 4].copy()
            output_file = "搴曢儴缁撴灉.xlsx"
            with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
                cond1_df.to_excel(writer, sheet_name='鏉′欢1-鍦伴噺', index=False)
                cond2_df.to_excel(writer, sheet_name='鏉′欢2-涓嶅垱鏂颁綆', index=False)
                cond3_df.to_excel(writer, sheet_name='鏉′欢3-浼板€间綆', index=False)
                cond4_df.to_excel(writer, sheet_name='鏉′欢4-搴曡儗绂?, index=False)
                cond2_plus_df.to_excel(writer, sheet_name='绗﹀悎2涓強浠ヤ笂', index=False)
                cond3_plus_df.to_excel(writer, sheet_name='绗﹀悎3涓強浠ヤ笂', index=False)
                all_conds_df.to_excel(writer, sheet_name='鍏ㄩ儴4涓潯浠?, index=False)
            print(f"\n缁熻缁撴灉锛?)
            print(f"  鏉′欢1锛堝湴閲忥級: {len(cond1_df)} 鍙?)
            print(f"  鏉′欢2锛堜笉鍒涙柊浣庯級: {len(cond2_df)} 鍙?)
            print(f"  鏉′欢3锛堜及鍊间綆锛? {len(cond3_df)} 鍙?)
            print(f"  鏉′欢4锛堝簳鑳岀锛? {len(cond4_df)} 鍙?)
            print(f"  绗﹀悎2涓強浠ヤ笂鏉′欢: {len(cond2_plus_df)} 鍙?)
            print(f"  绗﹀悎3涓強浠ヤ笂鏉′欢: {len(cond3_plus_df)} 鍙?)
            print(f"  鍏ㄩ儴4涓潯浠? {len(all_conds_df)} 鍙?)
            print(f"\n缁撴灉宸蹭繚瀛樺埌: {output_file}")
        else:
            print("\n鏈幏鍙栧埌鏈夋晥鑲＄エ鏁版嵁")
    except Exception as e:
        print(f"绋嬪簭鎵ц鍑洪敊: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()


