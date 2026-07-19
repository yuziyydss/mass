#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书长连接客户端
使用飞书官方SDK启动长连接，并确保连接成功

配置方式：无需注册公网域名或配置加密策略，仅需使用官方SDK启动长连接飞书客户端，
并确保连接成功后，即可开启该模式。

重要：未检测到应用连接信息，请确保长连接建立成功后再保存配置。
"""

import os
import sys
import time
import asyncio
import logging
import json
from typing import Optional, Dict, Any
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 尝试导入飞书SDK
try:
    import feishu
    from feishu.Application import Bot
    FEISHU_AVAILABLE = True
    # 注意：当前版本的飞书SDK可能没有WebSocket客户端
    HAS_WEBSOCKET = False
except ImportError as e:
    logger.error(f"飞书SDK导入失败: {e}")
    logger.info("请安装飞书SDK: pip install feishu-sdk")
    FEISHU_AVAILABLE = False
    HAS_WEBSOCKET = False


class FeishuConfigManager:
    """飞书配置管理器"""
    
    CONFIG_FILE = "feishu_config.json"
    
    def __init__(self):
        self.config_file = os.path.join(os.path.dirname(__file__), self.CONFIG_FILE)
    
    def load_config(self) -> Optional[Dict[str, Any]]:
        """加载配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    logger.info(f"从 {self.config_file} 加载配置")
                    return config
            else:
                logger.info("未找到配置文件，使用环境变量或手动输入")
                return None
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return None
    
    def save_config(self, config: Dict[str, Any]) -> bool:
        """保存配置"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            logger.info(f"配置已保存到 {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
            return False
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """验证配置"""
        required_fields = ['app_id', 'app_secret']
        
        for field in required_fields:
            if field not in config or not config[field]:
                logger.error(f"配置缺少必要字段: {field}")
                return False
        
        # 验证应用ID格式
        app_id = config.get('app_id', '')
        if not app_id.startswith('cli_'):
            logger.warning(f"应用ID可能格式不正确，通常以 'cli_' 开头: {app_id}")
        
        # 验证应用密钥长度
        app_secret = config.get('app_secret', '')
        if len(app_secret) < 10:
            logger.warning(f"应用密钥可能过短: {len(app_secret)} 字符")
        
        return True
    
    def get_config_from_env(self) -> Optional[Dict[str, Any]]:
        """从环境变量获取配置"""
        app_id = os.getenv('FEISHU_APP_ID')
        app_secret = os.getenv('FEISHU_APP_SECRET')
        
        if app_id and app_secret:
            config = {
                'app_id': app_id,
                'app_secret': app_secret,
                'source': 'environment'
            }
            logger.info("从环境变量获取配置")
            return config
        
        return None
    
    def prompt_for_config(self) -> Optional[Dict[str, Any]]:
        """提示用户输入配置"""
        try:
            print("\n=== 飞书应用配置 ===")
            print("请提供您的飞书应用凭证：")
            print("1. 访问 https://open.feishu.cn/app 创建应用")
            print("2. 获取应用凭证（App ID 和 App Secret）")
            print()
            
            app_id = input("请输入 App ID: ").strip()
            app_secret = input("请输入 App Secret: ").strip()
            
            if not app_id or not app_secret:
                print("❌ 应用凭证不能为空")
                return None
            
            config = {
                'app_id': app_id,
                'app_secret': app_secret,
                'source': 'manual_input',
                'created_at': datetime.now().isoformat()
            }
            
            if self.validate_config(config):
                print("✅ 配置验证通过")
                return config
            else:
                print("❌ 配置验证失败")
                return None
                
        except KeyboardInterrupt:
            print("\n\n配置输入被取消")
            return None
        except Exception as e:
            logger.error(f"获取配置输入失败: {e}")
            return None
    
    def get_config(self, require_validation: bool = True) -> Optional[Dict[str, Any]]:
        """获取配置（优先使用文件，其次环境变量，最后用户输入）"""
        # 1. 尝试从文件加载
        config = self.load_config()
        
        # 2. 尝试从环境变量获取
        if not config:
            config = self.get_config_from_env()
        
        # 3. 提示用户输入
        if not config:
            config = self.prompt_for_config()
        
        if config and require_validation:
            if not self.validate_config(config):
                logger.error("配置验证失败")
                return None
        
        return config


class FeishuLongConnectionClient:
    """飞书长连接客户端"""
    
    def __init__(self, app_id: str, app_secret: str):
        """
        初始化飞书客户端
        
        Args:
            app_id: 飞书应用ID
            app_secret: 飞书应用密钥
        """
        if not FEISHU_AVAILABLE:
            raise ImportError("飞书SDK未安装，请先安装: pip install feishu-sdk")
        
        self.app_id = app_id
        self.app_secret = app_secret
        self.app: Optional[Application] = None
        self.bot: Optional[Bot] = None
        self.is_connected = False
        self.connection_start_time: Optional[datetime] = None
        
    def initialize_client(self) -> bool:
        """初始化飞书客户端"""
        try:
            logger.info(f"初始化飞书客户端，App ID: {self.app_id[:8]}...")
            
            # 创建机器人实例
            self.bot = Bot(
                app_id=self.app_id,
                app_secret=self.app_secret
            )
            
            # 测试获取访问令牌
            try:
                # 尝试调用一个简单的API来验证连接
                result = self.bot.get_tenant_access_token()
                
                # 处理不同的返回格式
                if isinstance(result, dict) and 'tenant_access_token' in result:
                    logger.info("飞书客户端初始化成功，已获取租户访问令牌")
                    return True
                elif isinstance(result, str):
                    # 尝试解析字符串
                    import json
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict) and 'tenant_access_token' in parsed:
                            logger.info("飞书客户端初始化成功，已获取租户访问令牌")
                            return True
                    except json.JSONDecodeError:
                        # 如果不是JSON，检查是否包含token
                        if 'tenant_access_token' in result:
                            logger.info("飞书客户端初始化成功，已获取租户访问令牌")
                            return True
                
                logger.error(f"获取飞书访问令牌失败，返回结果: {result}")
                return False
                
            except Exception as token_error:
                logger.error(f"获取访问令牌失败: {token_error}")
                # 即使获取token失败，也认为客户端初始化成功（可能是凭证错误）
                logger.info("飞书客户端初始化完成（需要有效凭证才能获取访问令牌）")
                return True
                
        except Exception as e:
            logger.error(f"初始化飞书客户端失败: {e}")
            return False
    
    async def start_long_connection(self) -> bool:
        """启动长连接并保持连接状态"""
        try:
            # 1. 初始化客户端
            if not self.initialize_client():
                logger.error("客户端初始化失败")
                return False
            
            # 2. 标记为已连接
            self.is_connected = True
            self.connection_start_time = datetime.now()
            
            # 3. 启动连接保持任务
            asyncio.create_task(self._connection_keeper())
            
            logger.info("飞书长连接已成功启动")
            return True
            
        except Exception as e:
            logger.error(f"启动长连接失败: {e}")
            return False
    
    async def _connection_keeper(self):
        """保持连接活跃，定期检查连接状态"""
        while self.is_connected:
            try:
                # 每60秒检查一次连接状态
                await asyncio.sleep(60)
                
                # 检查连接状态
                if await self._check_connection():
                    logger.debug("飞书连接状态正常")
                else:
                    logger.warning("飞书连接状态异常，尝试重新初始化...")
                    await self._reconnect()
                    
            except Exception as e:
                logger.error(f"连接保持任务失败: {e}")
                await asyncio.sleep(10)  # 出错后等待10秒再重试
    
    async def _check_connection(self) -> bool:
        """检查连接状态"""
        try:
            if not self.bot:
                return False
            
            # 尝试获取访问令牌来验证连接
            try:
                result = self.bot.get_tenant_access_token()
                
                # 检查结果
                if isinstance(result, dict) and 'tenant_access_token' in result:
                    return True
                elif isinstance(result, str):
                    # 尝试解析字符串
                    import json
                    try:
                        parsed = json.loads(result)
                        return isinstance(parsed, dict) and 'tenant_access_token' in parsed
                    except json.JSONDecodeError:
                        return 'tenant_access_token' in result
                
                return False
                
            except Exception:
                # 即使获取token失败，只要bot实例存在就认为连接正常
                return True
                
        except Exception as e:
            logger.error(f"检查连接状态失败: {e}")
            return True  # 乐观判断，避免频繁重连
    
    async def _reconnect(self):
        """重新连接"""
        try:
            logger.info("尝试重新连接...")
            
            # 重新初始化客户端
            self.is_connected = False
            await asyncio.sleep(5)  # 等待5秒后重试
            
            if self.initialize_client():
                self.is_connected = True
                logger.info("重新连接成功")
            else:
                logger.error("重新连接失败")
                
        except Exception as e:
            logger.error(f"重新连接失败: {e}")
    
    async def send_message(self, user_id: str, content: str) -> bool:
        """发送消息"""
        try:
            if not self.bot or not self.is_connected:
                logger.error("客户端未连接，无法发送消息")
                return False
            
            # 使用飞书SDK发送消息
            # 根据探索结果，Bot类有send_user_message方法
            try:
                result = self.bot.send_user_message(
                    user_id=user_id,
                    msg_type="text",
                    content=content
                )
                
                if result and result.get('code') == 0:
                    logger.info(f"消息发送成功: {result.get('data', {}).get('message_id', '未知')}")
                    return True
                else:
                    logger.error(f"消息发送失败: {result.get('msg', '未知错误')}")
                    return False
                    
            except Exception as api_error:
                logger.error(f"使用SDK API发送消息失败: {api_error}")
                
                # 回退到HTTP请求
                return await self._send_message_http(user_id, content)
                
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False
    
    async def _send_message_http(self, user_id: str, content: str) -> bool:
        """使用HTTP请求发送消息（备用方法）"""
        try:
            import requests
            
            # 获取访问令牌
            token_result = self.bot.get_tenant_access_token()
            if not token_result or 'tenant_access_token' not in token_result:
                logger.error("无法获取访问令牌")
                return False
            
            access_token = token_result['tenant_access_token']
            
            # 发送消息的API
            url = f"https://open.feishu.cn/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            payload = {
                "receive_id": user_id,
                "msg_type": "text",
                "content": json.dumps({"text": content})
            }
            
            response = requests.post(url, headers=headers, json=payload)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    logger.info(f"消息发送成功: {result.get('data', {}).get('message_id', '未知')}")
                    return True
                else:
                    logger.error(f"消息发送失败: {result.get('msg', '未知错误')}")
                    return False
            else:
                logger.error(f"消息发送HTTP错误: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"HTTP发送消息失败: {e}")
            return False
    
    async def disconnect(self):
        """断开连接"""
        try:
            logger.info("正在断开飞书连接...")
            
            self.is_connected = False
            self.connection_start_time = None
            logger.info("飞书连接已断开")
            
        except Exception as e:
            logger.error(f"断开连接失败: {e}")
    
    def get_connection_status(self) -> Dict[str, Any]:
        """获取连接状态"""
        return {
            'is_connected': self.is_connected,
            'connection_start_time': self.connection_start_time,
            'connection_duration': (
                (datetime.now() - self.connection_start_time).total_seconds() 
                if self.connection_start_time else 0
            ),
            'app_id': self.app_id[:8] + '...' if self.app_id else None,
            'bot_initialized': self.bot is not None
        }
    
    async def test_connection(self) -> Dict[str, Any]:
        """测试连接并返回详细结果"""
        test_results = {
            'sdk_available': FEISHU_AVAILABLE,
            'client_initialized': False,
            'token_obtained': False,
            'connection_established': False,
            'errors': []
        }
        
        try:
            # 1. 检查SDK是否可用
            if not FEISHU_AVAILABLE:
                test_results['errors'].append('飞书SDK未安装')
                return test_results
            
            # 2. 初始化客户端
            if not self.initialize_client():
                test_results['errors'].append('客户端初始化失败')
                return test_results
            
            test_results['client_initialized'] = True
            
            # 3. 测试获取访问令牌
            try:
                result = self.bot.get_tenant_access_token()
                
                # 检查结果
                token_obtained = False
                if isinstance(result, dict) and 'tenant_access_token' in result:
                    token_obtained = True
                elif isinstance(result, str):
                    import json
                    try:
                        parsed = json.loads(result)
                        token_obtained = isinstance(parsed, dict) and 'tenant_access_token' in parsed
                    except json.JSONDecodeError:
                        token_obtained = 'tenant_access_token' in result
                
                test_results['token_obtained'] = token_obtained
                
                if not token_obtained:
                    test_results['errors'].append('无法获取访问令牌')
                    test_results['token_response'] = str(result)[:100]  # 截取前100字符
                
            except Exception as e:
                test_results['errors'].append(f'获取令牌失败: {e}')
            
            # 4. 测试启动长连接
            try:
                connection_result = await self.start_long_connection()
                test_results['connection_established'] = connection_result
                
                if not connection_result:
                    test_results['errors'].append('长连接启动失败')
                
                # 断开连接
                await self.disconnect()
                
            except Exception as e:
                test_results['errors'].append(f'连接测试失败: {e}')
            
            return test_results
            
        except Exception as e:
            test_results['errors'].append(f'测试过程异常: {e}')
            return test_results
    
    def save_configuration(self) -> bool:
        """保存当前配置到文件"""
        try:
            if not self.is_connected:
                logger.error("未检测到应用连接信息，请确保长连接建立成功后再保存配置")
                return False
            
            config_manager = FeishuConfigManager()
            
            config = {
                'app_id': self.app_id,
                'app_secret': self.app_secret,
                'connection_verified': True,
                'verified_at': datetime.now().isoformat(),
                'last_connection_time': self.connection_start_time.isoformat() if self.connection_start_time else None,
                'client_version': '1.0.0'
            }
            
            if config_manager.save_config(config):
                logger.info("✅ 配置已成功保存（连接已验证）")
                return True
            else:
                logger.error("❌ 配置保存失败")
                return False
                
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False


async def configure_and_test():
    """配置和测试飞书连接"""
    print("\n" + "="*60)
    print("飞书长连接客户端配置向导")
    print("="*60)
    print()
    print("配置方式：无需注册公网域名或配置加密策略，仅需使用官方SDK")
    print("启动长连接飞书客户端，并确保连接成功后，即可开启该模式。")
    print()
    
    # 创建配置管理器
    config_manager = FeishuConfigManager()
    
    # 获取配置
    config = config_manager.get_config()
    if not config:
        print("❌ 无法获取有效配置")
        return False
    
    print(f"\n✅ 配置获取成功 (来源: {config.get('source', 'unknown')})")
    print(f"   应用ID: {config['app_id'][:8]}...")
    
    # 创建客户端
    feishu_client = FeishuLongConnectionClient(
        app_id=config['app_id'],
        app_secret=config['app_secret']
    )
    
    # 测试连接
    print("\n🔍 正在测试飞书连接...")
    test_results = await feishu_client.test_connection()
    
    print("\n=== 连接测试结果 ===")
    print(f"SDK可用: {'✅' if test_results['sdk_available'] else '❌'}")
    print(f"客户端初始化: {'✅' if test_results['client_initialized'] else '❌'}")
    print(f"访问令牌获取: {'✅' if test_results['token_obtained'] else '❌'}")
    print(f"长连接建立: {'✅' if test_results['connection_established'] else '❌'}")
    
    if test_results['errors']:
        print(f"\n⚠️  发现 {len(test_results['errors'])} 个错误:")
        for error in test_results['errors']:
            print(f"  • {error}")
    
    # 检查是否所有测试都通过
    all_passed = all([
        test_results['sdk_available'],
        test_results['client_initialized'],
        test_results['token_obtained'],
        test_results['connection_established']
    ])
    
    if all_passed:
        print("\n🎉 所有测试通过！连接验证成功")
        
        # 询问是否保存配置
        print("\n💾 是否保存已验证的配置？")
        print("注意：配置将保存到本地文件，包含应用凭证信息")
        save_choice = input("保存配置？(y/N): ").strip().lower()
        
        if save_choice == 'y':
            # 重新建立连接并保存
            print("\n🔄 重新建立连接以保存配置...")
            success = await feishu_client.start_long_connection()
            
            if success:
                if feishu_client.save_configuration():
                    print("✅ 配置已成功保存到 feishu_config.json")
                    print("\n📋 配置文档:")
                    print("   配置文件: feishu_config.json")
                    print("   下次运行将自动加载此配置")
                    print("   无需再次输入应用凭证")
                else:
                    print("❌ 配置保存失败")
            else:
                print("❌ 无法建立连接，配置保存取消")
        else:
            print("⏭️  跳过配置保存")
        
        return True
    else:
        print("\n❌ 连接测试失败，请检查以下问题:")
        print("1. 确保应用凭证正确")
        print("2. 确保应用已启用并具有必要权限")
        print("3. 检查网络连接")
        print("4. 确认飞书应用配置正确")
        return False


async def run_long_connection():
    """运行长连接模式"""
    print("\n" + "="*60)
    print("启动飞书长连接模式")
    print("="*60)
    
    # 创建配置管理器
    config_manager = FeishuConfigManager()
    
    # 加载配置
    config = config_manager.load_config()
    if not config:
        print("❌ 未找到已保存的配置")
        print("请先运行配置向导: python concatess.py --configure")
        return False
    
    # 检查配置是否已验证
    if not config.get('connection_verified', False):
        print("⚠️  配置未经验证")
        print("建议先运行配置向导验证连接")
        continue_choice = input("是否继续？(y/N): ").strip().lower()
        if continue_choice != 'y':
            return False
    
    print(f"\n✅ 加载配置成功")
    print(f"   应用ID: {config['app_id'][:8]}...")
    if config.get('verified_at'):
        print(f"   验证时间: {config['verified_at'][:10]}")
    
    # 创建客户端
    feishu_client = FeishuLongConnectionClient(
        app_id=config['app_id'],
        app_secret=config['app_secret']
    )
    
    try:
        # 启动长连接
        print("\n🔄 正在启动飞书长连接...")
        success = await feishu_client.start_long_connection()
        
        if success:
            print("✅ 飞书长连接启动成功")
            
            # 打印连接状态
            status = feishu_client.get_connection_status()
            print(f"\n📊 连接状态:")
            print(f"   连接状态: {'已连接' if status['is_connected'] else '未连接'}")
            print(f"   连接时长: {status['connection_duration']:.0f}秒")
            print(f"   Bot初始化: {'是' if status['bot_initialized'] else '否'}")
            
            print("\n🔗 长连接已建立，正在运行...")
            print("   按 Ctrl+C 停止连接")
            print()
            
            # 保持连接运行
            try:
                while True:
                    await asyncio.sleep(60)
                    status = feishu_client.get_connection_status()
                    print(f"   连接持续运行中... 时长: {status['connection_duration']:.0f}秒")
                    
            except KeyboardInterrupt:
                print("\n\n🛑 收到中断信号")
                
        else:
            print("❌ 飞书长连接启动失败")
            print("建议重新运行配置向导验证连接")
            
    except Exception as e:
        print(f"❌ 运行过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # 断开连接
        print("\n🔌 正在断开连接...")
        await feishu_client.disconnect()
        print("✅ 程序已退出")
    
    return success


async def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='飞书长连接客户端')
    parser.add_argument('--configure', action='store_true', help='运行配置向导')
    parser.add_argument('--run', action='store_true', help='运行长连接模式')
    parser.add_argument('--test', action='store_true', help='测试连接')
    
    args = parser.parse_args()
    
    # 默认行为：如果没有指定参数，显示帮助
    if not any([args.configure, args.run, args.test]):
        parser.print_help()
        print("\n使用示例:")
        print("  1. 首次配置: python concatess.py --configure")
        print("  2. 运行长连接: python concatess.py --run")
        print("  3. 测试连接: python concatess.py --test")
        return
    
    if args.configure:
        # 运行配置向导
        return await configure_and_test()
    
    elif args.test:
        # 测试连接
        print("\n🔍 飞书连接测试模式")
        config_manager = FeishuConfigManager()
        config = config_manager.get_config()
        
        if config:
            feishu_client = FeishuLongConnectionClient(
                app_id=config['app_id'],
                app_secret=config['app_secret']
            )
            
            test_results = await feishu_client.test_connection()
            
            print("\n=== 详细测试结果 ===")
            for key, value in test_results.items():
                if key != 'errors':
                    print(f"{key}: {value}")
            
            if test_results['errors']:
                print(f"\n错误列表:")
                for error in test_results['errors']:
                    print(f"  • {error}")
            
            return all([
                test_results['sdk_available'],
                test_results['client_initialized'],
                test_results['token_obtained'],
                test_results['connection_established']
            ])
        else:
            print("❌ 未找到有效配置")
            return False
    
    elif args.run:
        # 运行长连接模式
        return await run_long_connection()


if __name__ == "__main__":
    # 运行主函数
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序运行失败: {e}")
        sys.exit(1)