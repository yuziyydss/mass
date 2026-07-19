import pandas as pd
import tushare as ts
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import numpy as np

# 从环境变量读取 Tushare Pro token
import os
token = os.getenv('TUSHARE_TOKEN') or os.getenv('TS_TOKEN')
if not token:
    raise RuntimeError('未找到 TUSHARE_TOKEN，请检查 .env')
ts.set_token(token)
pro = ts.pro_api()

# MASS 鎸囨爣鍙傛暟
N1 = 9
N2 = 25
M = 6

def get_stock_data(stock_code, days=90):
    """
    鑾峰彇鑲＄エ杩?0鏃ョ殑鍘嗗彶鏁版嵁
    :param stock_code: 鑲＄エ浠ｇ爜锛屽 '600519.SH'
    :param days: 鑾峰彇澶╂暟
    :return: 鍖呭惈鍘嗗彶鏁版嵁鐨凞ataFrame
    """
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days + 100)).strftime('%Y%m%d')  # 澶氳幏鍙栦竴浜涙暟鎹敤浜庤绠楁寚鏍?
    
    try:
        data = pro.daily(
            ts_code=stock_code,
            start_date=start_date,
            end_date=end_date
        )
        # 鎸夋棩鏈熸帓搴?
        data = data.sort_values('trade_date').reset_index(drop=True)
        # 杞崲鏃ユ湡鏍煎紡
        data['trade_date'] = pd.to_datetime(data['trade_date'])
        return data
    except Exception as e:
        print(f"鑾峰彇鑲＄エ鏁版嵁澶辫触: {e}")
        return None

def calculate_mass(data):
    """
    璁＄畻姊呮柉绾匡紙MASS锛夋寚鏍?
    :param data: 鑲＄エ鍘嗗彶鏁版嵁
    :return: 鍖呭惈MASS鎸囨爣鐨凞ataFrame
    """
    if data is None or len(data) < N2 + M + N1:
        return None
    
    # 璁＄畻鏈€楂樹环鍜屾渶浣庝环鐨勫樊鍊?
    high = data['high'].astype(float)
    low = data['low'].astype(float)
    hl = high - low
    
    # 璁＄畻EMA1鍜孍MA2
    ema1 = hl.ewm(span=N1, adjust=False).mean()
    ema2 = ema1.ewm(span=N1, adjust=False).mean()
    
    # 璁＄畻姣旂巼
    ratio = ema1 / ema2
    
    # 璁＄畻MASS
    mass = ratio.rolling(N2).sum()
    
    # 璁＄畻MASS鐨勭Щ鍔ㄥ钩鍧?
    mass_m = mass.rolling(M).mean()
    
    # 灏嗙粨鏋滄坊鍔犲埌DataFrame
    data['mass'] = mass
    data['mass_m'] = mass_m
    
    return data

def plot_stock_analysis(stock_code):
    """
    缁樺埗鑲＄エ鍒嗘瀽鍥捐〃锛屽寘鎷偂浠峰拰姊呮柉绾?
    :param stock_code: 鑲＄エ浠ｇ爜
    """
    # 璁剧疆涓枃瀛椾綋
    plt.rcParams['font.sans-serif'] = ['SimHei']  # 浣跨敤榛戜綋
    plt.rcParams['axes.unicode_minus'] = False  # 瑙ｅ喅璐熷彿鏄剧ず闂
    
    # 鑾峰彇鑲＄エ鏁版嵁
    data = get_stock_data(stock_code)
    if data is None:
        print("鏃犳硶鑾峰彇鑲＄エ鏁版嵁")
        return
    
    # 璁＄畻姊呮柉绾?
    data = calculate_mass(data)
    if data is None:
        print("鏃犳硶璁＄畻姊呮柉绾挎寚鏍?)
        return
    
    # 鍙繚鐣欒繎90澶╃殑鏁版嵁
    recent_data = data.tail(90)
    
    # 鍒涘缓鍥捐〃
    fig, ax1 = plt.subplots(figsize=(12, 6))
    
    # 缁樺埗鑲′环锛堟敹鐩樹环锛?
    color = 'tab:blue'
    ax1.set_xlabel('鏃ユ湡')
    ax1.set_ylabel('鏀剁洏浠?, color=color)
    ax1.plot(recent_data['trade_date'], recent_data['close'], color=color, label='鏀剁洏浠?)
    ax1.tick_params(axis='y', labelcolor=color)
    
    # 鍒涘缓绗簩涓獃杞达紝鐢ㄤ簬缁樺埗姊呮柉绾?
    ax2 = ax1.twinx()
    color = 'tab:red'
    ax2.set_ylabel('姊呮柉绾?, color=color)
    ax2.plot(recent_data['trade_date'], recent_data['mass_m'], color=color, label='姊呮柉绾?MASS)')
    ax2.tick_params(axis='y', labelcolor=color)
    
    # 娣诲姞鏍囬鍜屽浘渚?
    plt.title(f'{stock_code} 杩?0鏃ヨ偂浠蜂笌姊呮柉绾垮垎鏋?)
    fig.tight_layout()
    
    # 鏄剧ず鍥捐〃
    plt.show()

def main():
    """
    涓诲嚱鏁帮紝澶勭悊鐢ㄦ埛杈撳叆骞惰皟鐢ㄥ垎鏋愬嚱鏁?
    """
    print("鑲＄エ姊呮柉绾垮垎鏋愬伐鍏?)
    print("=" * 50)
    
    # 鑾峰彇鐢ㄦ埛杈撳叆鐨勮偂绁ㄤ唬鐮?
    stock_code = input("璇疯緭鍏ヨ偂绁ㄤ唬鐮侊紙濡傦細600519.SH锛? ")
    
    # 楠岃瘉鑲＄エ浠ｇ爜鏍煎紡
    if '.' not in stock_code:
        # 鑷姩娣诲姞鍚庣紑
        print("璇疯緭鍏ュ畬鏁寸殑鑲＄エ浠ｇ爜锛屽寘鍚氦鏄撴墍鍚庣紑锛堝锛?00519.SH锛?)
        return
    
    # 鎵ц鍒嗘瀽
    print(f"姝ｅ湪鍒嗘瀽 {stock_code} 杩?0鏃ユ暟鎹?..")
    plot_stock_analysis(stock_code)

if __name__ == "__main__":
    main()


