"""获取A股市场随机50只股票的梅斯线和股价波动情况，并使用DeepSeek AI进行分析"""

import os
import pandas as pd
import tushare as ts
from datetime import datetime, timedelta
from dotenv import load_dotenv
import random
import requests
import json
import time
from typing import List, Dict, Any

# 加载环境变量
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)
    print(f"已加载环境变量文件: {env_path}")
else:
    print(f"警告：未找到 .env 文件: {env_path}")

# 手动读取 .env 文件
print("\n开始手动读取 .env 文件...")
try:
    with open(env_path, 'r', encoding='utf-8') as f:
        content = f.read()
        print(f"文件内容:\n{content}")
        print("\n开始逐行处理...")
        
        f.seek(0)  # 重置文件指针
        for line_num, line in enumerate(f, 1):
            original_line = line
            line = line.strip()
            print(f"第{line_num}行: '{original_line}' -> 处理后: '{line}'")
            if line and '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                os.environ[key] = value
                print(f"  -> 设置环境变量: {key} = {value[:20]}..." if len(value) > 20 else f"  -> 设置环境变量: {key} = {value}")
            else:
                if not line:
                    print(f"  -> 跳过空行")
                elif line.startswith('#'):
                    print(f"  -> 跳过注释行")
                elif '=' not in line:
                    print(f"  -> 跳过无等号行")
except Exception as e:
    print(f"手动读取 .env 文件失败: {e}")
    import traceback
    traceback.print_exc()

# 设置 Tushare Pro token
token = os.getenv("TUSHARE_TOKEN") or os.getenv("TS_TOKEN")
if not token:
    raise RuntimeError("未找到 TUSHARE_TOKEN，请检查 .env")
ts.set_token(token)
pro = ts.pro_api()

# 获取DeepSeek API Key - 尝试多种可能的变量名
deepseek_api_key = os.getenv("siliconflow_API_KEY") or os.getenv("SILICONFLOW_API_KEY") or os.getenv("DEEPSEEK_API_KEY")

print(f"\n调试信息：TUSHARE_TOKEN = {'已找到' if token else '未找到'}")
print(f"调试信息：siliconflow_API_KEY = {'已找到' if os.getenv('siliconflow_API_KEY') else '未找到'}")
print(f"调试信息：SILICONFLOW_API_KEY = {'已找到' if os.getenv('SILICONFLOW_API_KEY') else '未找到'}")
print(f"调试信息：DEEPSEEK_API_KEY = {'已找到' if os.getenv('DEEPSEEK_API_KEY') else '未找到'}")
print(f"调试信息：最终使用的API Key = {'已找到' if deepseek_api_key else '未找到'}")

if not deepseek_api_key:
    print("\n可用的环境变量：")
    for key, value in os.environ.items():
        if 'SILICON' in key.upper() or 'DEEP' in key.upper() or 'API' in key.upper():
            print(f"  {key} = {value[:20]}..." if value and len(value) > 20 else f"  {key} = {value}")
    raise RuntimeError("未找到 siliconflow_API_KEY，请检查 .env")

# MASS 指标参数
N1 = 9
N2 = 25
M = 6

def get_random_stocks(count=50) -> pd.DataFrame:
    """获取随机50只股票"""
    stock_list = pro.stock_basic(
        exchange='',
        list_status='L',
        fields='ts_code,symbol,name,industry'
    )
    
    # 过滤掉创业板（3开头）、科创板（688开头）、北交所（8或9开头）
    stock_list = stock_list[~stock_list["symbol"].str.startswith("3")]
    stock_list = stock_list[~stock_list["symbol"].str.startswith("688")]
    stock_list = stock_list[~stock_list["symbol"].str.startswith("8")]
    stock_list = stock_list[~stock_list["symbol"].str.startswith("9")]
    
    # 随机选择50只股票
    if len(stock_list) > count:
        stock_list = stock_list.sample(n=count, random_state=42)
    
    print(f"随机选择了 {len(stock_list)} 只股票")
    return stock_list

def get_stock_data(stock_code: str, days=90) -> pd.DataFrame:
    """获取股票近90日的历史数据"""
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days + 100)).strftime('%Y%m%d')
    
    try:
        data = pro.daily(
            ts_code=stock_code,
            start_date=start_date,
            end_date=end_date
        )
        data = data.sort_values('trade_date').reset_index(drop=True)
        data['trade_date'] = pd.to_datetime(data['trade_date'])
        return data
    except Exception as e:
        print(f"获取{stock_code}数据失败：{e}")
        return None

def calculate_mass(data: pd.DataFrame) -> pd.DataFrame:
    """计算梅斯线（MASS）指标"""
    if data is None or len(data) < N2 + M + N1:
        return None
    
    high = data['high'].astype(float)
    low = data['low'].astype(float)
    hl = high - low
    
    ema1 = hl.ewm(span=N1, adjust=False).mean()
    ema2 = ema1.ewm(span=N1, adjust=False).mean()
    ratio = ema1 / ema2
    mass = ratio.rolling(N2).sum()
    mass_m = mass.rolling(M).mean()
    
    data['mass'] = mass
    data['mass_m'] = mass_m
    
    # 计算价格波动
    data['price_change'] = data['close'].pct_change() * 100
    data['volatility'] = data['price_change'].rolling(20).std()
    
    return data

def analyze_single_stock(stock_info: Dict[str, Any]) -> Dict[str, Any]:
    """分析单只股票"""
    ts_code = stock_info['ts_code']
    name = stock_info['name']
    
    data = get_stock_data(ts_code)
    if data is None:
        return None
    
    data = calculate_mass(data)
    if data is None:
        return None
    
    recent_data = data.tail(90)
    
    # 计算统计指标
    stats = {
        'ts_code': ts_code,
        'name': name,
        'industry': stock_info['industry'],
        'close_price': recent_data['close'].iloc[-1],
        'mass_value': recent_data['mass_m'].iloc[-1],
        'mass_trend': 'up' if recent_data['mass_m'].iloc[-1] > recent_data['mass_m'].iloc[-10] else 'down',
        'price_volatility': recent_data['volatility'].iloc[-1],
        'avg_price_change': recent_data['price_change'].mean(),
        'max_price': recent_data['high'].max(),
        'min_price': recent_data['low'].min(),
        'price_range': recent_data['high'].max() - recent_data['low'].min()
    }
    
    return stats

def analyze_with_deepseek(stock_data: List[Dict[str, Any]], max_retries=3) -> str:
    """使用DeepSeek AI分析数据，带重试机制"""
    # 准备数据摘要 - 减少数据量以避免超时
    data_summary = []
    for stock in stock_data:
        if stock is not None:
            data_summary.append({
                '代码': stock['ts_code'],
                '名称': stock['name'],
                '行业': stock['industry'],
                '收盘价': f"{stock['close_price']:.2f}",
                'MASS值': f"{stock['mass_value']:.2f}",
                'MASS趋势': stock['mass_trend'],
                '价格波动': f"{stock['price_volatility']:.2f}%",
                '平均涨跌': f"{stock['avg_price_change']:.2f}%",
                '价格区间': f"{stock['min_price']:.2f}-{stock['max_price']:.2f}",
                '价格振幅': f"{stock['price_range']:.2f}"
            })
    
    # 构建分析提示 - 只发送前15只股票的数据以减少超时风险
    prompt = f"""我分析了A股市场随机50只股票的梅斯线（MASS）和股价波动情况，请帮我分析这些数据并找出共性规律和特征。

数据摘要（前15只股票）：
{json.dumps(data_summary[:15], ensure_ascii=False, indent=2)}

请基于这些数据分析：
1. 梅斯线（MASS）指标与股价波动的关系
2. 梅斯线趋势与股价走势的关联性
3. 价格波动率与MASS值的统计特征
4. 适合的买卖点识别方法
5. 加仓和减仓的逻辑建议
6. 风险控制要点

请提供详细的分析结论和实用的交易建议。"""

    # 调用DeepSeek API - 增加超时时间到180秒
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {deepseek_api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "deepseek-ai/DeepSeek-V3",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }
    
    for attempt in range(max_retries):
        try:
            print(f"尝试调用DeepSeek API (第{attempt + 1}/{max_retries}次)...")
            response = requests.post(url, headers=headers, json=payload, timeout=180)
            response.raise_for_status()
            result = response.json()
            
            if 'choices' in result and len(result['choices']) > 0:
                return result['choices'][0]['message']['content']
            else:
                return "API返回格式异常"
        except requests.exceptions.Timeout:
            print(f"第{attempt + 1}次尝试超时，等待5秒后重试...")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return f"分析失败：API调用超时（已重试{max_retries}次）"
        except Exception as e:
            print(f"第{attempt + 1}次尝试失败：{str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return f"分析失败：{str(e)}（已重试{max_retries}次）"
    
    return "分析失败：未知错误"

def main():
    """主函数"""
    print("=" * 80)
    print("A股市场梅斯线分析工具")
    print("=" * 80)
    
    # 获取随机50只股票
    print("\n步骤1：获取随机50只股票...")
    stock_list = get_random_stocks(50)
    
    # 分析每只股票
    print("\n步骤2：分析每只股票的梅斯线和价格波动...")
    stock_data = []
    for i, (_, stock_info) in enumerate(stock_list.iterrows()):
        print(f"正在分析 ({i+1}/50): {stock_info['ts_code']} {stock_info['name']}")
        stats = analyze_single_stock(stock_info.to_dict())
        if stats is not None:
            stock_data.append(stats)
        
        # 添加延迟，避免API调用过于频繁
        import time
        time.sleep(0.1)
    
    # 保存原始数据
    print(f"\n步骤3：保存原始数据...")
    df = pd.DataFrame(stock_data)
    output_file = f"mass_analysis_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(output_file, index=False, encoding='utf-8-sig')
    print(f"原始数据已保存到: {output_file}")
    
    # 使用DeepSeek AI分析
    print(f"\n步骤4：使用DeepSeek AI分析数据...")
    print("这可能需要几分钟时间，请耐心等待...")
    
    analysis_result = analyze_with_deepseek(stock_data, max_retries=3)
    
    # 保存分析结果
    analysis_file = f"mass_analysis_report_{datetime.now().strftime('%Y%m%d')}.txt"
    with open(analysis_file, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write("A股市场梅斯线分析报告\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"分析股票数量: {len(stock_data)}\n\n")
        f.write("-" * 80 + "\n")
        f.write("DeepSeek AI 分析结果:\n")
        f.write("-" * 80 + "\n\n")
        f.write(analysis_result)
    
    print(f"\n分析报告已保存到: {analysis_file}")
    
    # 打印分析结果
    print("\n" + "=" * 80)
    print("DeepSeek AI 分析结果:")
    print("=" * 80)
    print(analysis_result)
    print("=" * 80)

if __name__ == "__main__":
    main()
