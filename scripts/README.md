# md_optimizer.py 使用说明

将指定目录下的英文 Markdown 文件批量翻译为中文，保持原始文档结构和格式不变。支持多目录并发处理。

## 依赖安装

```bash
pip install -r scripts/requirements.txt
```

## 前置条件

脚本通过 OpenAI 兼容接口调用模型，默认连接本地 `doubao2API` 服务：

- 地址：`http://127.0.0.1:7861/v1`
- API Key：`admin`

如需连接其他服务（如 OpenAI、Azure 等），通过 `--base-url` 和 `--api-key` 参数指定。

## 基本用法

```bash
# 翻译单个目录
python scripts/md_optimizer.py --input-dirs ./docs --output-dir ./docs_cn

# 翻译多个目录（并发处理）
python scripts/md_optimizer.py --input-dirs ./docs1 ./docs2 ./docs3 --output-dir ./output_cn

# 递归处理子目录
python scripts/md_optimizer.py --input-dirs ./docs --output-dir ./docs_cn --recursive

# 不指定输出目录时，默认在各输入目录下生成 _translated 子目录
python scripts/md_optimizer.py --input-dirs ./docs
```

## 参数说明

| 参数 | 简写 | 默认值 | 说明 |
|------|------|--------|------|
| `--input-dirs` | `-i` | 必填 | 一个或多个输入目录路径 |
| `--output-dir` | `-o` | `<input>/_translated` | 翻译结果输出目录 |
| `--base-url` | — | `http://127.0.0.1:7861/v1` | OpenAI 兼容 API 地址 |
| `--api-key` | — | `admin` | API 密钥 |
| `--model` | `-m` | `doubao` | 模型名称 |
| `--temperature` | `-t` | `0.3` | 生成温度，越低越保守 |
| `--recursive` | `-r` | 否 | 递归处理子目录 |
| `--concurrency` | `-c` | `3` | 最大并发请求数 |
| `--retry` | — | `3` | 单文件失败重试次数 |
| `--retry-delay` | — | `5.0` | 重试间隔秒数 |

## 输出目录结构

多目录模式下，各输入目录的翻译结果会以目录名为子目录存放在 `--output-dir` 下：

```
output_cn/
├── docs1/
│   └── guide.md
├── docs2/
│   └── api.md
└── docs3/
    └── faq.md
```

单目录模式下，翻译结果直接输出到 `--output-dir`，保持原有相对路径结构。

## 示例

```bash
# 连接 OpenAI 官方接口，使用 gpt-4o，并发数调为 5
python scripts/md_optimizer.py \
    --input-dirs ./en_docs \
    --output-dir ./zh_docs \
    --base-url https://api.openai.com/v1 \
    --api-key sk-xxxx \
    --model gpt-4o \
    --concurrency 5

# 递归翻译，失败最多重试 5 次
python scripts/md_optimizer.py \
    --input-dirs ./docs \
    --output-dir ./docs_cn \
    --recursive \
    --retry 5 \
    --retry-delay 3.0
```

## 翻译规则

- 代码块（` ``` `）和行内代码（`` ` `` ）内的代码**不翻译**，代码注释可翻译
- 超链接 URL **不翻译**，仅翻译链接文本
- `API`、`SDK`、`CLI` 等通用缩写保留英文
- 产品名、品牌名等专有名词保留原文或使用官方中文译名
- 所有 Markdown 格式标记（标题、表格、列表、加粗等）**完整保留**
