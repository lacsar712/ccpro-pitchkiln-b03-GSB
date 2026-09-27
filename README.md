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

**业务规则**（唯一事实来源：`apps/kiln/services/floor_rules.py`）：

1. **软化点合法性**：`softPointC` 须为 **正数且 ≤ 120℃**，校验函数为 `validate_soft_point`。非法值在两条写路径上以**同一中文文案**拒绝（「软化点须为正数且不超过 120℃。」）：
   - **写路径一 · 抽屉表单**：`SoftPointProbeForm.clean_softPointC`（`add_probe` 视图）；
   - **写路径二 · 服务层保存**：`floor_rules.register_probe`（视图与任何脚本/后台调用都走它）。
   模型字段同时挂同一 validator，只改表单而不改服务层（或反之）都算未修到位。
2. **出胶资格**：相位切到 `drawing`（出胶）时，进行中的 CookRun 须至少一条**合格探针**——合法且 `softPointC ≤ 95`（`qualified_probes` / `assert_can_enter_drawing`）。抽屉时间线的 ok/hot 着色（`SoftPointProbe.drawing_qualified`）与改相位读取的最新合格探针（`latest_qualified_probe`）来自同一条规则与同一排序，写入探针后立刻反映，不会与时间线分叉。
3. **图例计数**：看板图例按相位实时计数，「出胶」数只含相位已是 `drawing` 的灶，与网格中出胶瓦片复算一致。

## 界面

- 首页：**灶台值守看板** — 左侧班次条 + 按过道排布的灶台瓦片；点瓦片打开右侧抽屉（值守、探针时间线、改相位 / 登记探针 / 开灶）
- 次页：**来脂批** — 卡片时间线，非宽表 CRUD

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有灶台则只保证账号存在。样例地名仅用「松脂坳 / 桐油坑」系。其中「坳火-甲」（保温中）故意缺合格探针（两条探针均 > 95℃），用于演示出胶资格的拒绝路径。

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
