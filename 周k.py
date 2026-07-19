﻿﻿﻿"""鎵惧嚭甯傚満鍐呮湰鍛ㄤ笅璺屼絾璧勯噾娴佸叆鐨勮偂绁紙浣跨敤鏃绾挎暟鎹級"""

import os
import pandas as pd
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
    """鑾峰彇鎵€鏈夎偂绁ㄥ垪琛?""
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
        return df
    except Exception as e:
        print(f"鑾峰彇{ts_code}鏃绾垮け璐ワ細{e}")
        return pd.DataFrame()


def get_daily_money_flow(pro: ts.pro_api, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """鑾峰彇鏃ヤ富鍔涜祫閲戞祦鍏ユ祦鍑烘暟鎹?""
    try:
        daily_flow = pro.moneyflow(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,buy_elg_vol,sell_elg_vol,buy_lg_vol,sell_lg_vol'
        )
        
        if daily_flow.empty:
            return pd.DataFrame()
        
        daily_flow['main_inflow'] = daily_flow['buy_elg_vol'] + daily_flow['buy_lg_vol']
        daily_flow['main_outflow'] = daily_flow['sell_elg_vol'] + daily_flow['sell_lg_vol']
        daily_flow['main_net_in'] = daily_flow['main_inflow'] - daily_flow['main_outflow']
        
        return daily_flow
    except Exception as e:
        print(f"鑾峰彇{ts_code}鏃ヤ富鍔涜祫閲戞祦鍏ユ祦鍑哄け璐ワ細{e}")
        return pd.DataFrame()


def main():
    """涓诲嚱鏁?""
    try:
        pro = init_client()
        print("瀹㈡埛绔垵濮嬪寲鎴愬姛")
        
        # 浣跨敤鏈懆鐨勬棩鏈熻寖鍥达細2026-02-23 鍒?2026-02-27
        this_week_start = datetime(2026, 2, 23)
        this_week_end = datetime(2026, 2, 27)
        this_week_start_str = this_week_start.strftime('%Y%m%d')
        this_week_end_str = this_week_end.strftime('%Y%m%d')
        
        # 鑾峰彇杩囧幓30澶╃殑鏁版嵁锛岀敤浜庤绠楁定璺屽箙
        end_date = this_week_end.strftime('%Y%m%d')
        start_date = (this_week_end - timedelta(days=30)).strftime('%Y%m%d')
        
        print(f"鏈懆鏃ユ湡鑼冨洿锛歿this_week_start_str} 鍒?{this_week_end_str}")
        print(f"鑾峰彇鏁版嵁鐨勬棩鏈熻寖鍥达細{start_date} 鍒?{end_date}")
        
        stock_list = get_stock_list(pro)
        print(f"鑾峰彇鍒?{len(stock_list)} 鍙偂绁?)
        
        result = []
        analyzed_count = 0
        error_count = 0
        
        for _, stock in stock_list.iterrows():
                
            ts_code = stock['ts_code']
            name = stock['name']
            
            if ts_code.endswith('.BJ'):
                analyzed_count += 1
                continue
            
            try:
                # 鑾峰彇杩囧幓30澶╃殑鏃绾挎暟鎹?
                daily_kline = get_daily_kline(pro, ts_code, start_date, end_date)
                if daily_kline.empty:
                    if analyzed_count < 10:
                        print(f"{ts_code} {name}: 鏃绾挎暟鎹负绌?)
                    error_count += 1
                    continue
                
                # 鎸夋棩鏈熸帓搴?
                daily_kline = daily_kline.sort_values('trade_date')
                
                # 鎵惧埌鏈懆鐨勬暟鎹紙2026-02-23鍒?026-02-27锛?
                this_week_data = daily_kline[
                    (daily_kline['trade_date'] >= this_week_start_str) & 
                    (daily_kline['trade_date'] <= this_week_end_str)
                ]
                
                # 鎵惧埌涓婂懆鐨勬暟鎹細浣跨敤鏈懆涔嬪墠鐨?涓氦鏄撴棩
                last_week_data = daily_kline[daily_kline['trade_date'] < this_week_start_str].tail(5)
                
                # 濡傛灉鏈懆鎴栦笂鍛ㄦ暟鎹负绌猴紝璺宠繃
                if this_week_data.empty or last_week_data.empty:
                    if analyzed_count < 10:
                        if this_week_data.empty:
                            print(f"{ts_code} {name}: 鏈懆鏁版嵁涓虹┖")
                        else:
                            print(f"{ts_code} {name}: 涓婂懆鏁版嵁涓虹┖")
                    error_count += 1
                    continue
                
                # 璁＄畻鏈懆鍜屼笂鍛ㄧ殑骞冲潎鏀剁洏浠?
                this_week_avg_close = this_week_data['close'].mean()
                last_week_avg_close = last_week_data['close'].mean()
                
                # 璁＄畻鏈懆娑ㄨ穼骞?
                week_change = (this_week_avg_close - last_week_avg_close) / last_week_avg_close * 100
                
                # 濡傛灉鏈懆涓嬭穼
                if week_change < 0:
                    # 鑾峰彇鏈懆鐨勮祫閲戞祦鍚戞暟鎹?
                    this_week_flow = get_daily_money_flow(pro, ts_code, this_week_start_str, this_week_end_str)
                    if this_week_flow.empty:
                        if analyzed_count < 10:
                            print(f"{ts_code} {name}: 鏈懆璧勯噾娴佸悜鏁版嵁涓虹┖")
                        error_count += 1
                        continue
                    
                    # 璁＄畻鏈懆涓诲姏璧勯噾姹囨€?
                    main_inflow = this_week_flow['main_inflow'].sum()
                    main_outflow = this_week_flow['main_outflow'].sum()
                    net_mf = main_inflow - main_outflow
                    
                    # 濡傛灉涓诲姏鍑€娴佸叆
                    if net_mf > 0:
                        print(f"鍙戠幇鐩爣鑲＄エ: {ts_code} {name}锛屾湰鍛ㄨ穼{week_change:.2f}%锛屼富鍔涙祦鍏main_inflow:.2f}涓囷紝涓诲姏娴佸嚭{main_outflow:.2f}涓囷紝鍑€娴佸叆{net_mf:.2f}涓?)
                        result.append({
                            'ts_code': ts_code,
                            'name': name,
                            'week_change': week_change,
                            'main_inflow': main_inflow,
                            'main_outflow': main_outflow,
                            'main_net_in': net_mf
                        })
            except Exception as e:
                if analyzed_count < 10:
                    print(f"{ts_code} {name}: 寮傚父閿欒 - {str(e)}")
                error_count += 1
            finally:
                analyzed_count += 1
                
                if analyzed_count % 50 == 0:
                    print(f"宸插垎鏋?{analyzed_count} 鍙偂绁紝杩涘害锛歿analyzed_count/len(stock_list)*100:.1f}%")
                
                import time
                time.sleep(0.5)
        
        print(f"\n鍒嗘瀽瀹屾垚锛屽叡鍒嗘瀽 {analyzed_count} 鍙偂绁紝鍏朵腑 {error_count} 鍙嚭鐜伴敊璇?)
        
        if result:
            result_df = pd.DataFrame(result)
            result_df = result_df.sort_values('main_net_in', ascending=False)
            print("\n鏈懆涓嬭穼浣嗕富鍔涘噣娴佸叆鐨勮偂绁細")
            print(result_df)
            
            output_file = f"week_down_money_in_{this_week_end_str}.csv"
            result_df.to_csv(output_file, index=False, encoding='utf-8-sig')
            print(f"\n缁撴灉宸蹭繚瀛樺埌 {output_file}")
        else:
            print("\n鏈壘鍒版湰鍛ㄤ笅璺屼絾涓诲姏鍑€娴佸叆鐨勮偂绁?)
    except Exception as e:
        print(f"绋嬪簭杩愯鍑洪敊锛歿e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()


