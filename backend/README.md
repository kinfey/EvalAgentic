# 商品列表 Flask Demo

## 运行

先安装依赖，确保默认运行命令具备 Flask：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
PORT=8081 python app.py
```

打开 `http://localhost:8081` 查看页面。

## Docker 运行

```bash
docker build -t products-flask-demo .
docker run --rm -p 8081:8081 products-flask-demo
```
