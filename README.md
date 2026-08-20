# Delta Cardkey Solver

自动完成 Platoboost Delta Key System的captcha验证→获取key的全流程求解器

## 安装

```bash
pip install -r requirements.txt
```

## 使用方法

### 1. 直接求解已有 ticket

```bash
#从 auth 链接
python main.py "https://auth.platorelay.com/a?d=<ticket>"

#从原始 ticket 字符串
python main.py "<ticket>"

#从文件读取（一行一个ticket）
python main.py tickets.txt
```

### 2. 生成测试链接并求解

```bash
# 生成 5 条测试链接并求解
python main.py --generate 5

# 生成 3 条，静默模式
python main.py --generate 3 --quiet

# 只生成测试链接不求解
python main.py --generate 5 --no-auto
```

### 3. 作为库使用

```python
from captcha_solver import solve, session
import requests

# 获取 captcha 挑战
s = session()
ch = s.get('https://captcha.platorelay.com/api/challenge').json()

# 下载图片
img = requests.get(
    'https://captcha.platorelay.com' + ch['image'],
    headers={'Referer': 'https://captcha.platorelay.com/'}
).content

#求解
x, y = solve(img, ch['type'])
print(f'答案: ({x}, {y})')

#提交答案
r = s.post('https://captcha.platorelay.com/api/answer', json={
    'challenge_id': ch['challenge_id'],
    'x': x, 'y': y
}).json()
print(f'成功: {r["success"]}, token: {r.get("token")}')
```

```python
from auth_client import extract_ticket, do_step, create_session
from captcha_solver import solve, session as captcha_session

#提取ticket
ticket = extract_ticket("https://auth.platorelay.com/a?d=...")

#创建会话
s = create_session()

#获取 captcha 并求解
cs = captcha_session()
ch = cs.get('https://captcha.platorelay.com/api/challenge').json()
img = cs.get('https://captcha.platorelay.com' + ch['image'],
             headers={'Referer': 'https://captcha.platorelay.com/'}).content
x, y = solve(img, ch['type'])
r = cs.post('https://captcha.platorelay.com/api/answer', json={
    'challenge_id': ch['challenge_id'], 'x': x, 'y': y
}).json()
token = r['token']

#推进checkpoint
result = do_step(ticket, token, service=3)
print(result)
```

## 命令行参数

| 参数 | 说明 |
|------|------|
| `target` | auth URL / ticket 字符串 / 文件路径 |
| `--generate N` / `-g N` | 通过 Platoboost API 生成 N 条链接 |
| `--quiet` / `-q` | 静默模式，只输出结果 |
| `--max-rounds N` | 最大求解轮数（默认 3） |
| `--no-auto` | 只生成链接，不解 |

## 模块说明

| 文件 | 功能 |
|------|------|
| `main.py` | 主入口，全自动求解链路 |
| `captcha_solver.py` | captcha图片求解器 |
| `auth_client.py` | auth服务客户端，AES-CTR加密ticket操作 step推进 |
| `link_generator.py` | 从Platoboost API生成测试auth链接 |
| `requirements.txt` | Python依赖 |

## 求解流程

```
ticket→获取metadata→求解captcha→推进step→解码回调 →下一张ticket→...→获取key
```

每轮自动从metadata获取service参数 captcha和metadata并行获取以减少耗时

## 依赖

- Python 3.10+
- numpy, scipy, Pillow — 图片处理
- pycryptodome — AES-CTR
- requests, urllib3
- numba — 加速

## License

MIT
