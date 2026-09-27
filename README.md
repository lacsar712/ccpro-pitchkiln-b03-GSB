# PitchKiln-01 · 灶台值守看板

Django 5 + PostgreSQL：灶台瓦片看板 + 右侧抽屉探针时间线，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX：局部刷新灶台网格与抽屉
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4710** |
| Postgres | **6110**（容器内 5432） |

数据库账号：`pitchkiln` / `pitchkiln` / 库名 `pitchkiln`

## 快速启动

```bash
cd PitchKiln/PitchKiln-01
docker compose up --build -d
```

浏览器打开：http://localhost:4710

演示账号：

- `admin` / `123456`（超级用户）
- `worker` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 6110，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6110
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4710
```

## 业务模型

1. **ResinLot（来脂批）**：`lotCode`、`originPlace`、`arrivalKg`、`receivedAt`
2. **FireHearth（灶台）**：`lane`、`tag`（唯一）、`resinGrade`、相位 `cold|charging|ramping|holding|drawing`
3. **CookRun（熬制值守）**：归属灶台与来脂批、`openedAt`、`closedAt`（可空）、`targetSoftPointC`
4. **SoftPointProbe（软化点探针）**：归属值守、`sampledAt`、`softPointC`、`samplerName`

**业务规则**（全部收敛在 `apps/kiln/services/floor_rules.py`，是唯一数据源）：

- **探针合法性**：软化点必须为正数且 `0 < softPointC ≤ 120`。两条写路径共用同一个 `validate_soft_point()`，非法值中文拒绝文案只有一份（`INVALID_SOFT_POINT_MESSAGE`）：
  1. **抽屉表单路径**：`SoftPointProbeForm.clean_softPointC()` 调 `validate_soft_point()`；
  2. **服务层路径**：视图保存与种子/脚本都走 `record_probe()`，内部先过同一校验再落库（种子 `seed.py` 即经此写入）。
  
  因此绕过表单直接写库同样会被拒绝，且文案与表单完全一致。
- **出胶资格**：进入 `drawing`（出胶）时，进行中 CookRun 的探针时间线上必须存在 `softPointC ≤ 95` 的合格探针。时间线统一按 `-sampledAt, -id` 排序，「最新合格探针」`latest_qualifying_probe()` 沿同一条时间线取第一条合格者，抽屉时间线、改相位校验、看板瓦片读数全部同源，不分叉。探针写入后资格立即生效（同一事务读已提交数据），无需刷新。
- **图例与过滤**：看板图例各相位计数只统计当前相位即为该相位的灶（出胶计数只含已在出胶的灶）；点击图例按 `?phase=` 过滤，过滤瓦片与图例计数来自同一份 hearth 列表复算，天然对齐。

## 界面

- 首页：**灶台值守看板** — 左侧班次条 + 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，非宽表 CRUD

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有灶台则只保证账号存在。样例地名仅用「松脂坳 / 桐油坑」系。

种子探针均经服务层 `record_probe()` 写入（演示非表单写路径同源校验）。5 个灶台中进行中的值守里**恰好 `坳火-乙`（升温）一条合格探针都没有**（最新 96.20℃ > 95，不能切出胶）；保温灶 `坳火-甲`、出胶灶 `坑火-西一`、装料灶 `坳火-夜班` 均有 ≤95℃ 合格探针，可直接演示出胶资格。

## 目录结构

```
PitchKiln-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/
  apps/kiln/          # 模型、视图、floor_rules、种子
  templates/floor/    # 值守看板 + 抽屉
  templates/resin/    # 来脂批时间线
  static/css/         # 值守台 ops-console 样式
```
