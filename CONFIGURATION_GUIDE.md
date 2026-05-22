# 飞书长连接客户端配置指南

## 配置方式说明

**无需注册公网域名或配置加密策略，仅需使用官方 SDK 启动长连接飞书客户端，并确保连接成功后，即可开启该模式。**

**重要提示：未检测到应用连接信息，请确保长连接建立成功后再保存配置。**

## 快速开始

### 1. 安装依赖
```bash
pip install feishu-sdk
```

### 2. 运行配置向导
```bash
python concatess.py --configure
```

### 3. 运行长连接
```bash
python concatess.py --run
```

## 详细配置步骤

### 步骤1：获取飞书应用凭证

1. 访问 [飞书开放平台](https://open.feishu.cn/app)
2. 创建新应用或选择现有应用
3. 获取以下信息：
   - **App ID** (应用ID，通常以 `cli_` 开头)
   - **App Secret** (应用密钥)

### 步骤2：配置应用权限

在飞书开放平台为应用配置以下权限：
- 获取应用访问凭证
- 发送消息
- 接收消息（如需要）

### 步骤3：运行配置向导

运行配置向导时，系统会：
1. 提示输入应用凭证
2. 自动测试连接
3. 验证所有功能
4. **仅在连接成功后才允许保存配置**

### 步骤4：保存配置

当所有测试通过后，系统会询问是否保存配置。配置将保存到 `feishu_config.json` 文件，包含：
- 应用ID和密钥
- 连接验证状态
- 验证时间戳
- 客户端版本信息

## 配置文件说明

### 配置文件位置
```
feishu_config.json
```

### 配置文件内容示例
```json
{
  "app_id": "cli_xxxxxxxx",
  "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "connection_verified": true,
  "verified_at": "2026-03-08T12:00:00.000000",
  "last_connection_time": "2026-03-08T12:00:00.000000",
  "client_version": "1.0.0",
  "source": "manual_input"
}
```

### 配置文件字段说明
- `app_id`: 飞书应用ID
- `app_secret`: 飞书应用密钥
- `connection_verified`: 连接是否已验证（**重要：只有为true时才表示配置有效**）
- `verified_at`: 验证时间戳
- `last_connection_time`: 最后连接时间
- `client_version`: 客户端版本
- `source`: 配置来源（environment/manual_input/file）

## 使用模式

### 1. 配置模式 (`--configure`)
```bash
python concatess.py --configure
```
- 首次配置时使用
- 测试连接并验证所有功能
- **仅在连接成功后保存配置**

### 2. 运行模式 (`--run`)
```bash
python concatess.py --run
```
- 使用已保存的配置运行长连接
- 自动加载 `feishu_config.json`
- 检查配置是否已验证
- 建立并保持长连接

### 3. 测试模式 (`--test`)
```bash
python concatess.py --test
```
- 测试当前配置的连接状态
- 显示详细的测试结果
- 不保存配置

## 连接验证流程

### 验证步骤
1. **SDK检查**: 验证飞书SDK是否可用
2. **客户端初始化**: 使用提供的凭证初始化客户端
3. **令牌获取**: 测试获取访问令牌
4. **长连接建立**: 测试建立长连接
5. **配置保存**: **仅在以上所有步骤成功后**才允许保存配置

### 验证失败处理
如果验证失败，系统会显示：
- 具体的失败原因
- 建议的解决方案
- 重新配置的选项

## 环境变量支持

除了配置文件，也支持环境变量：

```bash
# 设置环境变量
export FEISHU_APP_ID='cli_xxxxxxxx'
export FEISHU_APP_SECRET='xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

# 运行程序
python concatess.py --run
```

**优先级顺序**：
1. 配置文件 (`feishu_config.json`)
2. 环境变量 (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`)
3. 手动输入

## 安全注意事项

1. **保护应用凭证**: 配置文件包含敏感信息，请妥善保管
2. **验证状态**: 只有 `connection_verified: true` 的配置才是有效的
3. **定期更新**: 建议定期验证连接状态
4. **权限最小化**: 为应用配置最小必要权限

## 故障排除

### 常见问题

#### 1. "SDK未安装"错误
```bash
pip install feishu-sdk
```

#### 2. "无法获取访问令牌"错误
- 检查应用ID和密钥是否正确
- 确认应用已启用
- 检查网络连接

#### 3. "长连接启动失败"错误
- 确认应用具有必要权限
- 检查飞书API状态
- 验证网络连接

#### 4. "配置未经验证"警告
- 运行配置向导重新验证
- 检查配置文件中的 `connection_verified` 字段

### 调试模式
```bash
# 查看详细日志
python concatess.py --test
```

## 高级配置

### 自定义配置文件路径
修改 `FeishuConfigManager` 类中的 `CONFIG_FILE` 变量。

### 扩展功能
可以在 `FeishuLongConnectionClient` 类中添加：
- 消息接收处理
- 事件订阅
- 自定义业务逻辑

## 技术支持

- 飞书开放平台文档: https://open.feishu.cn/document/
- SDK文档: https://github.com/liyao2598330/feishu-sdk
- 问题反馈: 检查日志文件或联系技术支持

---

**重要提醒：配置保存前必须确保长连接建立成功，否则配置可能无效。**